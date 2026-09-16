"""运行时状态：持久化、会话上下文、限流。

改造前这些东西全是 bot.py 里的模块级裸字典 —— 进程一退全丢（群属性昨晚切的猫娘模式，
今早开机就没了），而且 session key 只增不减。这里统一收口成四个组件：

  StateStore    原子落盘 + 定时刷盘 + 优雅退出兜底，跨重启保留群模式/上下文/额度计数
  SessionStore  每个会话一份滑动窗口上下文，带过期 + 并发锁
  TokenBucket   群级令牌桶限流（按用户要求，刻意不做 per-user 配额）
  DailyBudget   全局每日调用上限，跨重启保留
"""

import asyncio
import json
import os
import tempfile
import time
from collections import deque

STATE_VERSION = 1


def atomic_write_json(path, data):
    """原子写：先写临时文件再 os.replace，避免进程被杀时留下半个损坏的 json。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ══════════════════════════════ 状态持久化 ══════════════════════════════


class StateStore:
    """跨重启保命的状态仓库。

    collectors 是一组回调，落盘前调用它们把各处内存态灌进 data 字典，
    避免 SessionStore 之类的组件自己再关心 IO。
    """

    def __init__(self, path, save_interval=30.0):
        self.path = path
        self.save_interval = save_interval
        self._collectors = []
        self._dirty = False
        self._task = None
        self.data = {
            "version": STATE_VERSION,
            "modes": {},        # group_id -> mode
            "sessions": {},     # session_id -> {"messages": [...], "last_time": float}
            "buffers": {},      # group_id -> [{"sender","text","time"}]
            "cooldowns": {},    # group_id -> last random-reply timestamp
            "relations": {},    # "group|member" -> 关系档案，见 relations.py
            "digests": {},      # group_id -> 群聊长期记忆，见 digest.py
            "promises": {},     # group_id -> 承诺账本，见 digest.PromiseBook
            "groups": {},       # group_id -> {"last_seen": ts}，用来知道该往哪些群主动发言
            "usage": {"day": "", "used": 0},
        }
        self.load()

    def register_collector(self, fn):
        self._collectors.append(fn)

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"⚠️ 状态文件损坏，已忽略并从空状态启动: {e}")
            return
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            print("⚠️ 状态文件版本不匹配，已忽略")
            return
        for key in self.data:
            if key in raw:
                self.data[key] = raw[key]

    def mark_dirty(self):
        self._dirty = True

    def _build_snapshot(self):
        for fn in self._collectors:
            try:
                fn(self.data)
            except Exception as e:
                print(f"⚠️ 状态收集器异常: {e}")
        return self.data

    def save_now(self, force=False):
        if not self._dirty and not force:
            return False
        self._build_snapshot()
        atomic_write_json(self.path, self.data)
        self._dirty = False
        return True

    async def flush_loop(self):
        """后台定时刷盘；被取消时最后再补一次，防止刚标的脏数据丢掉。"""
        try:
            while True:
                await asyncio.sleep(self.save_interval)
                if self._dirty:
                    try:
                        self.save_now()
                    except Exception as e:
                        print(f"⚠️ 状态落盘失败: {e}")
        except asyncio.CancelledError:
            self.save_now()
            raise

    def start_background_flush(self):
        if self._task is None:
            self._task = asyncio.get_event_loop().create_task(self.flush_loop())
        return self._task

    # ── 群模式 ──
    def get_mode(self, group_id, default="normal"):
        return self.data["modes"].get(group_id, default)

    def set_mode(self, group_id, mode):
        self.data["modes"][group_id] = mode
        self.mark_dirty()


# ══════════════════════════════ 会话上下文 ══════════════════════════════


def trim_messages(messages, max_messages, max_chars):
    """按「用户+助手」成对裁剪，保证上下文从一条 user 话开头。

    两条两条地丢，是因为不成对的开头（第一条就 assistant）会让模型困惑；
    同时用字数而不是条数做最终兜底，防止一波长文本把 token 打爆。
    """
    msgs = list(messages)
    if max_messages and len(msgs) > max_messages:
        msgs = msgs[-max_messages:]

    def total_chars():
        return sum(len(str(m.get("content") or "")) for m in msgs)

    while len(msgs) > 2 and total_chars() > max_chars:
        msgs = msgs[2:]

    while msgs and msgs[0].get("role") != "user":
        msgs = msgs[1:]
    return msgs


class SessionStore:
    """每人每会话一份上下文，socket:[messages] 滑动窗口 + TTL + 并发锁。

    同一个 session 的两条消息可能并发进来（群友连发两条），如果各自读写历史会互相覆盖，
    所以每个 session 配一把锁，调用方在【取上下文 → 调模型 → 写回】全程持锁。
    副作用是把同一会话的请求串行化了，但群聊场景下这反而保证回复顺序不会错乱。
    """

    def __init__(self, max_turns=8, max_chars=3000, ttl=1800.0, max_sessions=2000):
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._sessions = {}
        self._locks = {}

    def lock(self, session_id):
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _live(self, session_id, now):
        sess = self._sessions.get(session_id)
        if not sess:
            return None
        if now - sess.get("last_time", 0) > self.ttl:
            # 静默太久，当作换了话题，别把陈年旧账串进来
            sess["messages"] = []
            return sess
        return sess

    def build_messages(self, session_id, system_prompt, user_text,
                       memory_block="", turn_context="", now=None):
        """组装本次请求的消息列表。调用方需持有该 session 的锁。

        顺序是刻意的：**越稳定的越靠前**，前缀缓存才吃得满（实测命中率 85% → 98%）。
            稳定头 → 每日记忆 → 会话历史 → 本轮动态信封 + 用户原话
        任何随「谁在说 / 说到第几句」变化的内容都不能混进前面的块，否则它之后
        的内容每一轮都作废，缓存等于白开。

        注意：turn_context 只拼在发出去的请求里，不进历史 —— record() 存的是原话，
        所以信封不会一轮轮堆积。
        """
        now = time.time() if now is None else now
        sess = self._live(session_id, now)
        history = trim_messages(
            sess.get("messages", []) if sess else [],
            self.max_turns * 2,
            self.max_chars,
        )
        out = [{"role": "system", "content": system_prompt}]
        if memory_block:
            out.append({"role": "system", "content": memory_block})
        out.extend(history)
        out.append({
            "role": "user",
            "content": f"{turn_context}\n\n{user_text}" if turn_context else user_text,
        })
        return out

    def record(self, session_id, user_text, reply, now=None):
        """把这一轮问答写回滑动窗口。调用方需持有该 session 的锁。"""
        now = time.time() if now is None else now
        sess = self._sessions.get(session_id)
        if sess is None or now - sess.get("last_time", 0) > self.ttl:
            sess = {"messages": [], "last_time": now}
            self._sessions[session_id] = sess
        sess["messages"] = trim_messages(
            sess["messages"] + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ],
            self.max_turns * 2,
            self.max_chars,
        )
        sess["last_time"] = now

    def clear_group(self, group_id):
        """清掉这个群的全部会话历史，返回清掉的会话数。

        切换模式时用：光改 system prompt 是不够的 —— 窗口里还堆着上一个状态的回复，
        模型会照着最近几轮自己的语气继续说话，表现出来就是「刚切完只带一点风味，两句话又回去了」。
        """
        prefix = f"{group_id}_"
        killed = [sid for sid in self._sessions if sid.startswith(prefix)]
        for sid in killed:
            self._sessions.pop(sid, None)
        return len(killed)

    def rename_user(self, group_id, old, new):
        """把该群会话历史里出现的旧称呼统一换成新称呼，返回改写的条数。

        改名只改档案是不够的：历史里那句「以后叫你【阿龙】」还在窗口里，
        模型读到它就继续叫旧名 —— 用户看到的现象是「刚改完它又忘了」。
        单字名字一律不替换（「颖」这种替换起来误伤太大）。
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if len(old) < 2 or not new or old == new:
            return 0
        prefix = f"{group_id}_"
        hits = 0
        for sid, sess in self._sessions.items():
            if not sid.startswith(prefix):
                continue
            for msg in sess.get("messages", []):
                content = msg.get("content") or ""
                if old in content:
                    msg["content"] = content.replace(old, new)
                    hits += 1
        return hits

    def prune(self, now=None):
        """清理过期会话，防止长期运行下 session key 无界增长。"""
        now = time.time() if now is None else now
        expired = [
            sid for sid, sess in self._sessions.items()
            if now - sess.get("last_time", 0) > self.ttl * 2
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            self._locks.pop(sid, None)

        if len(self._sessions) > self.max_sessions:
            oldest = sorted(
                self._sessions.items(), key=lambda kv: kv[1].get("last_time", 0)
            )[: len(self._sessions) - self.max_sessions]
            for sid, _ in oldest:
                self._sessions.pop(sid, None)
                self._locks.pop(sid, None)
        return len(expired)

    def hydrate(self, raw: dict):
        """从落盘状态恢复。"""
        restored = 0
        for sid, sess in (raw or {}).items():
            if not isinstance(sess, dict):
                continue
            self._sessions[sid] = {
                "messages": trim_messages(
                    sess.get("messages") or [], self.max_turns * 2, self.max_chars
                ),
                "last_time": float(sess.get("last_time") or 0),
            }
            restored += 1
        if restored:
            self.prune()
        return restored

    def dump_into(self, data: dict):
        data["sessions"] = {
            sid: {"messages": s["messages"], "last_time": s["last_time"]}
            for sid, s in self._sessions.items()
        }

    def stats(self):
        return {
            "sessions": len(self._sessions),
            "messages": sum(len(s["messages"]) for s in self._sessions.values()),
        }


# ══════════════════════════════ 限流 ══════════════════════════════


class TokenBucket:
    """群级令牌桶。注意：按需求刻意【没有】per-user 维度。"""

    def __init__(self, capacity, refill_seconds, max_keys=5000):
        self.capacity = float(capacity)
        self.refill_seconds = float(refill_seconds)
        self.max_keys = max_keys
        self._tokens = {}  # key -> [tokens, last_ts]

    def try_acquire(self, key, cost=1.0, now=None):
        now = time.time() if now is None else now
        tokens, last = self._tokens.get(key, [self.capacity, now])
        tokens = min(self.capacity, tokens + max(0.0, now - last) / self.refill_seconds)
        if tokens >= cost:
            self._tokens[key] = [tokens - cost, now]
            self._evict_idle(now)
            return True, 0.0
        self._tokens[key] = [tokens, now]
        return False, (cost - tokens) * self.refill_seconds

    def _evict_idle(self, now, idle_seconds=3600.0):
        if len(self._tokens) <= self.max_keys:
            return
        stale = [k for k, v in self._tokens.items() if now - v[1] > idle_seconds]
        for k in stale:
            self._tokens.pop(k, None)
        # 极端情况下还超限，就丢最久没用的
        if len(self._tokens) > self.max_keys:
            for k, _ in sorted(self._tokens.items(), key=lambda kv: kv[1][1])[
                : len(self._tokens) - self.max_keys
            ]:
                self._tokens.pop(k, None)


class DailyBudget:
    """全局每日调用额度。计数写进持久化字典，所以重启不会「重置」当天用量。"""

    def __init__(self, limit, state: dict):
        self.limit = limit
        self._state = state

    @staticmethod
    def today():
        return time.strftime("%Y-%m-%d", time.localtime())

    @property
    def used(self):
        return self._state.get("used", 0) if self._state.get("day") == self.today() else 0

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def try_consume(self, cost=1):
        today = self.today()
        if self._state.get("day") != today:
            self._state["day"] = today
            self._state["used"] = 0
        if self._state.get("used", 0) + cost > self.limit:
            return False
        self._state["used"] = self._state.get("used", 0) + cost
        return True
