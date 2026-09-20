"""群聊长期记忆：把「大伙最近在聊啥」沉淀成一份会滚动更新的摘要。

为什么不一次性读一周的原文
--------------------------
表面上很诱人（语料全在嘛），但代价是：输入随群活跃度线性膨胀，一个吵闹的群一周能堆到几十万
tokens；更要命的是模型对长文本的「中段遗忘」，一整周的原文塞进去，真正值得记的梗反而被稀释掉。

这里采用增量滚动压缩：
    新摘要 = f(旧摘要, 当天新增)
每次输入都是「上一版摘要 + 一段有界的新增」，因此开销恒定，与群有多吵无关；
而信息是逐步累积的，一周后的摘要里仍然留着周一发生的事。

当天的量实在太大时（超过一屏），先分块压成要点（map），再合并成最终摘要（reduce），
这样单次请求的上下文永远可控。

产出是「一段人话 + 一份结构化数据」，一次调用同时拿到两样东西：
    正文：《群史记》风格的幽默简报，可以直接发给群友看
    标签：<DIGEST>{...}</DIGEST>，机器可读的话题/金句/承诺/人物，留着以后做催债、点名

「当天新增」从哪来
------------------
不再自己维护待压缩队列，而是直接读 archive.py 里的时间戳大于上次压缩时刻的原文。
单一数据源的好处：压缩失败不会丢消息（下次重读），重启不用管队列持久化，
state.json 也不必再塞几千条聊天记录。
"""

import json
import re
import time

import naming

DIGEST_TAG = re.compile(r"<DIGEST>(.*?)</DIGEST>", re.S)

# 一次压缩最多看多少字符（超出就走分块）。0.8 元/百万 tokens，1 万字 ≈ 1 分钱，主要是防失控。
DEFAULT_CHUNK_CHARS = 6000
DEFAULT_MAX_BLOCKS = 10

SYSTEM_MAP = """你在为 QQ 群整理聊天记录。下面是一段原始聊天记录，请从中提取要点。

只提取【真实出现过】的内容，绝不许脑补或推测。输出 4~8 条要点，每条不超过 25 字，形如：
- 谁 在聊 什么（一句话说清）
- 出现了什么好玩的梗/金句（照抄原话）
- 谁 许下了什么承诺 / 说要做什么事（请客、发资源、还钱等），哪怕只是一句玩笑
- 谁 对「{bot}」明显热情/示好，或明显在抬杠/阴阳/钓鱼（照抄最能说明态度的那一句）

没有的内容就不要写。不要写总结性的套话，不要评价。

【敏感内容一律不复述 —— 这是硬规则，优先于上面所有条目】
原始记录里若出现政治性内容（人名、事件、谐音或缩写暗号、地域/族群歧视）、涉黄涉暴，
一律**不抄原话、不改写、不缩写、不做联想、不解释**，一个字都不要留在产出里。
这类内容至多概括成一句事实层面的「有人在群里试探敏感话题（不复述）」，且整份产出里只记一条。
自检标准：把这句话原样截图发到群外，会不会给{bot}的使用者惹麻烦？会，就不写。
宁可这一整条要点不写，也不要为了「记录完整」把它抄进去。"""

SYSTEM_REDUCE = """你在为 QQ 群「{bot}」整理长期记忆。你会拿到【上一版摘要】和【这一段时间的新要点】，
请把它们合并成一份新的长期记忆，并输出两段内容。

第 1 段：《群史记》风格的幽默简报，用{bot}（贴吧老哥乐子人）的口吻，150 字以内。
要求：像群里最八卦的人在做周报，可以有吐槽和调侃，但别伤人；没发生什么就直说这阵子群里安静得离谱。

第 2 段：换行后单独输出一行机器指令，严格形如：
<DIGEST>{"topics":[{"topic":"话题","who":["阿强"],"heat":3}],"memes":[{"quote":"原话","why":"好笑在哪"}],"promises":[{"who":"阿强","what":"说要请全群喝奶茶","due":""}],"people":[{"who":"阿强","note":"最近在打黑猴"}],"affinity":[{"who":"阿强","delta":2,"why":"一整天都在捧场接梗"}]}</DIGEST>

字段规则：
- topics：近期话题，heat 用 1~5 表示热度。最多 6 条。
- memes：值得复用的金句/梗，quote 尽量照抄原话。最多 5 条。
- promises：没有兑现的承诺或待办，这是为了以后「催债」用的，玩笑也算。最多 5 条。
- people：对具体某个人的新认知（在玩什么、什么处境）。最多 6 条。
- affinity：**这一段时间里，每个人跟「{bot}」相处给你的整体印象变化**。delta 取 -3~3 的整数：
  正 = 更熟络/更客气/愿意接梗/主动搭话；负 = 抬杠、冒犯、阴阳怪气、反复钓鱼。
  这是唯一会决定「{bot}日后对某人什么态度」的字段，所以只依据记录里真实发生过的互动来判，
  不许凭印象编。这段时间没露过面、或态度没明显变化的人，不要写进来（宁缺勿滥）。
- 只写记录里真实出现过的内容，绝不许编造。没有的字段给空数组 []。
- **敏感内容不复述（硬规则）**：政治性人名/事件/谐音暗号、地域族群歧视、涉黄涉暴，一律不进这份记忆 ——
  包括 memes 的 quote、people 的 note、topics 的 topic 名、以及第 1 段的《群史记》。
  memes 是「照抄原话」字段，最容易被顺手带进去，务必逐条筛。
  这类内容至多在 topics 里留一条「敏感话题试探（不复述）」，不写是谁、不写内容。
  上一版摘要里若已带有这类内容，合并时**顺手删掉**，不要因为「它是旧记录」就留着。
- 合并时：仍然新鲜的保留，已经过时/完结的丢掉，保持总量不膨胀。
- 压缩稿里的署名有时会写成「一位群友」：那是**系统给不出名字的人**共用的泛称，
  不是谁的外号也不是真名。别把它当成某一个人来总结，也别写进 people / promises 的 who；
  真要提到就说「某位群友」，不要复读这个泛称。
- 标签行之后不要输出任何内容。"""


# 说话人没有名字时，压缩稿里写什么。
#
# ⚠️ 旧版写的是 openid 后四位（`sender[-4:]`）。后果不是「不好看」而是**泄漏**：
# 摘要里从此出现「6ABA」这种人名，而摘要每轮注入 prompt —— 模型当群里真有个人叫 6ABA，
# 照着复读，甚至拿去叫人。这跟「回复里冒出 openid 尾巴」是同一类事故，只是入口在压缩侧。
# 没有名字就用通用词兜住：区分度差一点，但绝不把编号喂进模型嘴里。
# ⚠️ 这个字面同时写进了上面的 SYSTEM_REDUCE（要让模型知道它是个泛称），改一处要改两处。
UNAMED_WHO = "一位群友"


def render_transcript(entries, resolver=None, max_chars=DEFAULT_CHUNK_CHARS):
    """把消息渲染成给模型看的文本。有昵称就用昵称，没有也不硬编。"""
    lines = []
    used = 0
    for e in entries:
        who = (resolver(e["sender"]) if resolver else None) or UNAMED_WHO
        text = (e.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        row = f"[{who}] {text}"
        lines.append(row)
        used += len(row) + 1
        if used >= max_chars:
            break
    return "\n".join(lines)


def split_digest(raw):
    """拆成 (可读正文, 结构化 dict)。解析失败不影响正文。"""
    if not raw:
        return "", None
    m = DIGEST_TAG.search(raw)
    body = DIGEST_TAG.sub("", raw).strip()
    if not m:
        return body or raw.strip(), None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return body, None
    return body, data if isinstance(data, dict) else None


def normalize(data):
    """保证各字段都是列表，别把脏数据带进状态文件。"""
    out = {"topics": [], "memes": [], "promises": [], "people": [], "affinity": []}
    if not isinstance(data, dict):
        return out
    for key in out:
        val = data.get(key)
        if isinstance(val, list):
            # affinity 是「一批人」，不是「几条事」，上限给宽一点
            cap = 8 if key == "affinity" else 6
            out[key] = [v for v in val if isinstance(v, dict)][:cap]
    return out


def render_summary(state, mode="normal"):
    """把摘要渲染成能发到群里的文本，0 token。"""
    if not state or not state.get("brief") and not state.get("data"):
        if mode == "discipline":
            return "（翻开班级日志）本班近期的档案还是空白的，值日生无从查起。"
        return "（翻了翻群史记）这一阵子群里安静得离谱，连个能记的槽点都没攒下。"

    parts = []
    if state.get("brief"):
        parts.append(f"📜 【群史记】{state['brief']}")
    data = state.get("data") or {}
    if data.get("topics"):
        parts.append("🔥 最近在聊：" + "、".join(
            str(t.get("topic") or "") for t in data["topics"] if t.get("topic")))
    if data.get("promises"):
        parts.append("🧾 还没兑现：" + "；".join(
            f"{p.get('who','有人')}——{p.get('what','')}" for p in data["promises"] if p.get("what")))
    if data.get("memes"):
        best = data["memes"][0].get("quote") if isinstance(data["memes"][0], dict) else ""
        if best:
            parts.append(f"😂 名场面：「{best}」")
    return "\n".join(parts)


def render_people(state, limit=8):
    """把摘要里的人物档案渲染成「谁：什么事」的名册，注入给模型用（0 token）。

    ⚠️ 为什么单独加这一块（2026-09-20 实测）：人物档案此前**从不注入**，模型只能读
    【群史记】里那种连动长句（「A 号刚解封就忙着跟 B 贴贴飙车」），读的时候把主语拧成
    另一个人（把「家豪解封」说成「老王解封」），再把错的存进会话反复引用。
    「谁：什么事」一行一人，主语想拧都没得拧。
    只喂给模型，不进 render_summary（那个是发到群里的版本，群友不需要看档案）。
    """
    data = (state or {}).get("data") or {}
    people = [p for p in (data.get("people") or [])
              if isinstance(p, dict) and p.get("who") and p.get("note")]
    if not people:
        return ""
    lines = "\n".join(f"- {p['who']}：{p['note']}" for p in people[:limit])
    return ("👥 这阵子你观察到的几个人（系统整理的档案，谁的条目说的就是谁的事，"
            "引用前对准名字，别张冠李戴）：\n" + lines)


class GroupDigest:
    """每个群一份「当前摘要 + 上次压缩到哪」。

    待压缩的消息不再存在这里 —— 它们住在 archive.py 的 JSONL 里，
    靠 last_run 这个水位线切分「已压缩 / 待压缩」。
    """

    def __init__(self, chunk_chars=DEFAULT_CHUNK_CHARS, max_blocks=DEFAULT_MAX_BLOCKS,
                 max_entries=2000):
        self.chunk_chars = chunk_chars
        self.max_blocks = max_blocks
        self.max_entries = max_entries
        self.groups = {}   # group_id -> {"summary":{...}, "last_run": ts}

    # ── 存取 ──
    def _slot(self, group_id):
        slot = self.groups.get(group_id)
        if slot is None:
            slot = {"summary": None, "last_run": 0.0}
            self.groups[group_id] = slot
        return slot

    def hydrate(self, raw):
        restored = 0
        for gid, obj in (raw or {}).items():
            if not isinstance(obj, dict):
                continue
            slot = self._slot(gid)
            summary = obj.get("summary")
            if isinstance(summary, dict):
                slot["summary"] = {
                    "brief": summary.get("brief") or "",
                    "data": normalize(summary.get("data")),
                    "updated": float(summary.get("updated") or 0),
                }
            slot["last_run"] = float(obj.get("last_run") or 0)
            restored += 1
        return restored

    def dump_into(self, data):
        data["digests"] = {
            gid: {"summary": s["summary"], "last_run": s["last_run"]}
            for gid, s in self.groups.items()
        }

    def touch(self, group_id):
        """登记一个群，让它进入定时扫描范围（没有任何状态也要登记，否则新群永远不会被压缩）。"""
        return self._slot(group_id)

    def last_run(self, group_id):
        return self.groups.get(group_id, {}).get("last_run", 0.0)

    def mark_compressed(self, group_id, ts=None):
        self._slot(group_id)["last_run"] = time.time() if ts is None else ts

    def needs_compress(self, group_id, interval_hours, min_entries, pending_count=None,
                       count_trigger=None):
        """该不该压：攒够了就压，兜底每天至少一次。

        count_trigger 是「攒够这么多条就压」—— 活跃的群不该干等 24 小时才更新记忆和好感度。
        interval_hours 是兜底：冷清的群只要攒够 min_entries，也保证一天压一次。

        pending_count 由调用方从归档算出来传进来（这里不持数据）。
        """
        slot = self.groups.get(group_id)
        if not slot:
            return False
        if pending_count is None:
            return False
        if pending_count < min_entries:
            return False
        if pending_count >= self.max_entries:
            return True      # 撑满了，立刻压，不等时间
        if count_trigger and pending_count >= count_trigger:
            return True      # 攒够了，不等时间
        return time.time() - slot.get("last_run", 0.0) >= interval_hours * 3600.0

    def compress_reason(self, group_id, interval_hours, pending_count, count_trigger=None):
        """给日志用的一句话，说明这次为什么压（省得事后猜）。0 token。"""
        if pending_count >= self.max_entries:
            return "队列已满"
        if count_trigger and pending_count >= count_trigger:
            return f"攒够 {pending_count} 条"
        idle_h = (time.time() - (self.groups.get(group_id) or {}).get("last_run", 0.0)) / 3600.0
        return f"每日兜底（距上次 {idle_h:.1f}h）"

    def get(self, group_id):
        return (self.groups.get(group_id) or {}).get("summary")

    def rename_in_memory(self, group_id, old, new):
        """把长期记忆里固化的旧称呼一并改掉，返回替换处数。

        每日压缩写的是「当时的昵称」，改名后摘要里还留着旧名，而摘要每轮都注入 prompt，
        等于又把旧名送回模型嘴边 —— 这是「明明改了名它照样叫旧的」的第二个源头。
        单字名字不替换（误伤面太大）。
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if len(old) < 2 or not new or old == new:
            return 0
        summary = (self.groups.get(group_id) or {}).get("summary")
        if not isinstance(summary, dict):
            return 0
        counter = [0]

        def walk(node):
            if isinstance(node, str):
                if old in node:
                    counter[0] += node.count(old)
                    return node.replace(old, new)
                return node
            if isinstance(node, list):
                return [walk(x) for x in node]
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            return node

        summary["brief"] = walk(summary.get("brief") or "")
        summary["data"] = walk(summary.get("data"))
        return counter[0]

    # ── 压缩 ──
    def build_blocks(self, slot, resolver=None):
        """把待压缩区切成若干块。每块控制在 chunk_chars 以内。"""
        blocks, cur, size = [], [], 0
        for e in slot["pending"]:
            who = (resolver(e["sender"]) if resolver else None) or UNAMED_WHO
            row_len = len(who) + len(e["text"]) + 4
            if size + row_len > self.chunk_chars and cur:
                blocks.append(cur)
                cur, size = [], 0
            cur.append(e)
            size += row_len
        if cur:
            blocks.append(cur)
        if len(blocks) > self.max_blocks:
            blocks = blocks[-self.max_blocks:]   # 极端活跃时只保最近的，宁可丢旧的也不失控
        return blocks

    def old_summary_text(self, slot, resolver=None, refresh=None):
        """回喂给模型的上一版摘要。

        刻意**不含 affinity**：那一栏是「已结算过的好感度变化」，喂回去模型会照着再写一遍，
        等于同一批互动被反复扣分/加分。它只存在档案里备查，不参与下一轮合并。

        refresh 用来把上一版里的旧称呼改写成当前称呼 —— 这一版是要喂给模型的，
        带着旧名进去，模型下一版还会照着写，旧名就永远滚不掉。
        """
        s = slot.get("summary")
        if not s:
            return ""
        parts = [f"[上一版简报] {s.get('brief','')}"]
        data = s.get("data") or {}
        if data.get("topics"):
            parts.append("[上一版话题] " + "、".join(
                str(t.get("topic") or "") for t in data["topics"]))
        if data.get("promises"):
            parts.append("[上一版待兑现] " + "；".join(
                f"{p.get('who','')}:{p.get('what','')}" for p in data["promises"]))
        if data.get("people"):
            parts.append("[上一版人物] " + "；".join(
                f"{p.get('who','')}:{p.get('note','')}" for p in data["people"]))
        text = "\n".join(p for p in parts if p.strip())
        return refresh(text) if refresh else text


# ══════════════════════════ 压缩流程 ══════════════════════════


async def compress_group(store, group_id, entries, ask, resolver=None, budget=None, log=None,
                         refresh=None):
    """把一批新消息合并进长期摘要。

    entries: 待压缩的原始消息（来自 archive.since），元素形如 {"sender","text","ts"}
    ask(system, user) -> str | None   发起一次模型调用
    budget() -> bool                  可选，返回 False 表示今天的额度用完了
    refresh(text) -> str              可选，把回喂文本里的旧称呼换成当前称呼
    块数 <= 1 时直接压；否则先分块提取要点（map），再合并（reduce）。
    返回新的 summary，压缩失败返回 None —— 此时调用方不该推进 last_run，
    这样消息会留在待压缩区，下一轮再读一遍，不会丢。
    """
    slot = store._slot(group_id)
    if not entries:
        return None

    blocks = store.build_blocks({"pending": entries}, resolver)
    if len(blocks) <= 1:
        new_text = render_transcript(entries, resolver, max_chars=10 ** 9)
    else:
        points = []
        for block in blocks:
            if budget and not budget():
                if log:
                    log("🛑 额度不足，本次分块摘要中断")
                return None
            text = render_transcript(block, resolver, max_chars=10 ** 9)
            out = await ask(naming.render(SYSTEM_MAP), text)
            if out:
                points.append(out.strip())
        if not points:
            return None
        new_text = "\n".join(points)
        if log:
            log(f"🗜️  {len(blocks)} 块 → 要点已提取，正在合并")

    old = store.old_summary_text(slot, resolver, refresh=refresh)
    payload = f"{old}\n\n[这段时间的新内容]\n{new_text}" if old else f"[这段时间的新内容]\n{new_text}"

    if budget and not budget():
        if log:
            log("🛑 额度不足，本次摘要跳过")
        return None

    raw = await ask(naming.render(SYSTEM_REDUCE), payload)
    if not raw:
        return None

    body, data = split_digest(raw)
    if not body and not data:
        return None

    slot["summary"] = {
        "brief": body[:400],
        "data": normalize(data),
        "updated": time.time(),
    }
    return slot["summary"]


# ══════════════════════════ 承诺账本 ══════════════════════════

# 催债文案：让模型照着当前人设说一句，比模板有人味。一天最多一两次，成本可忽略。
# 调用方负责 naming.render 之后再用 —— 人设里的自称不能写死。
SYSTEM_DUN = """你在 QQ 群里扮演「{bot}」，一个爱吐槽、嘴贫但心地不坏的群友。
下面列了几条群里立下却还没兑现的承诺（也可能是玩笑话）。请你挑其中【最该催的那一条】，
用你的口吻写一句催债的话，1~2 句，要自然、要搞笑、要点名道姓，可以带点调侃但不要真伤人。
不要解释，不要列清单，就只输出这一句话。"""


def _norm_promise_key(who, what):
    """去重键：去掉空白和标点，避免「请喝奶茶」和「请 喝奶茶」记成两条。"""
    s = f"{who or ''}|{what or ''}".lower()
    return re.sub(r"[\s，,。.、!！?？;；:：'\"“”‘’]", "", s)[:80]


def parse_due(text, now=None):
    """把模型给的 due 文本尽量解成时间戳；解不出来返回 None，交给宽限期兜底。

    只处理最常见的几种说法，不追求完备 —— 解不出来顶多是晚点催，不会出错。
    """
    if not text:
        return None
    now = time.time() if now is None else now
    t = str(text).strip()

    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        try:
            return time.mktime(time.strptime(
                f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} 12:00",
                "%Y-%m-%d %H:%M"))
        except Exception:
            return None

    CN_NUM = {"两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
              "九": 9, "十": 10, "半": 1}

    if "后天" in t:
        return now + 2 * 86400.0
    if "明天" in t or "明早" in t or "明晚" in t:
        return now + 86400.0
    if "下周" in t:
        return now + 7 * 86400.0

    # (?!前) 是为了别把「两天前」当成「两天后」
    m = re.search(r"([0-9两二三四五六七八九十]+)\s*天(?!前)[后内]?", t)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else CN_NUM.get(raw, 0)
        if n:
            return now + n * 86400.0
    m = re.search(r"([0-9两二三四五六七八九十]+)\s*周[后内个]", t)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else CN_NUM.get(raw, 0)
        if n:
            return now + n * 7 * 86400.0

    m = re.search(r"(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", t)
    if m:
        table = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
                 "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
        want = table.get(m.group(1))
        if want is not None:
            cur = time.localtime(now).tm_wday
            delta = (want - cur) % 7 or 7
            return now + delta * 86400.0

    m = re.search(r"(\d{1,2})\s*[号日]", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            lt = time.localtime(now)
            try:
                ts = time.mktime((lt.tm_year, lt.tm_mon, day, 12, 0, 0, 0, 0, lt.tm_isdst))
            except Exception:
                return None
            return ts if ts > now else ts + 30 * 86400.0
    return None


class PromiseBook:
    """每群一份「说好了却没兑现」的账本。

    数据是摘要产出的副产品：每次压缩完用 sync() 对齐一遍 —— 摘要里新出现的加进来，
    摘要里已经消失的（被模型判定为完结/过时）自动撤下。这样账本会自己收敛，
    不需要额外的模型调用去判断「这条兑现了没」。

    催债有次数上限：催满 PROMISE_MAX_NAG 次就自己撤下。
    追着人反复要奶茶那不是幽默，是骚扰。
    """

    def __init__(self, grace_hours=24.0, nag_interval_hours=48.0, max_nag=2, max_items=12):
        self.grace_hours = float(grace_hours)
        self.nag_interval_hours = float(nag_interval_hours)
        self.max_nag = int(max_nag)
        self.max_items = int(max_items)
        self.items = {}   # group_id -> [promise, ...]

    # ── 存取 ──
    def hydrate(self, raw):
        restored = 0
        for gid, rows in (raw or {}).items():
            if not isinstance(rows, list):
                continue
            kept = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                kept.append({
                    "id": r.get("id") or _norm_promise_key(r.get("who"), r.get("what")),
                    "who": r.get("who") or "有人",
                    "what": r.get("what") or "",
                    "since": float(r.get("since") or time.time()),
                    "due_ts": r.get("due_ts"),
                    "due_text": r.get("due_text") or "",
                    "nag": int(r.get("nag") or 0),
                    "last_nag": float(r.get("last_nag") or 0),
                })
            if kept:
                self.items[gid] = kept
                restored += len(kept)
        return restored

    def dump_into(self, data):
        data["promises"] = {gid: rows for gid, rows in self.items.items() if rows}

    def list(self, group_id):
        return self.items.get(group_id) or []

    def sync(self, group_id, promises, now=None):
        """用新摘要里的承诺列表对齐账本。返回 (新增, 撤下)。"""
        now = time.time() if now is None else now
        fresh = {}
        for p in promises or []:
            if not isinstance(p, dict):
                continue
            what = str(p.get("what") or "").strip()
            if not what:
                continue
            who = str(p.get("who") or "有人").strip()[:20]
            key = _norm_promise_key(who, what)
            if key in fresh:
                continue
            fresh[key] = {
                "id": key, "who": who, "what": what[:80], "since": now,
                "due_ts": parse_due(p.get("due"), now),
                "due_text": str(p.get("due") or ""),
                "nag": 0, "last_nag": 0.0,
            }

        old = {r["id"]: r for r in self.items.get(group_id, [])}
        merged = []
        added = 0
        for key, row in fresh.items():
            prev = old.get(key)
            if prev:
                merged.append(prev)          # 已存在的保留催债次数，别被重置
            else:
                merged.append(row)
                added += 1
        # 摘要里不再出现 = 模型认为过时或已完结，撤下
        dropped = len(old) - sum(1 for k in old if k in fresh)

        merged.sort(key=lambda r: r.get("since") or 0)
        self.items[group_id] = merged[: self.max_items]
        return added, max(0, dropped)

    # ── 催债 ──
    def dunnable(self, group_id, now=None):
        """该催的承诺：过了宽限期，且离上次催够久，且还没催满上限。"""
        now = time.time() if now is None else now
        grace = self.grace_hours * 3600.0
        gap = self.nag_interval_hours * 3600.0
        out = []
        for r in self.list(group_id):
            if r.get("nag", 0) >= self.max_nag:
                continue
            start = r.get("due_ts") or (r.get("since", 0) + grace)
            if now < start:
                continue
            if r.get("last_nag") and now - r["last_nag"] < gap:
                continue
            out.append(r)
        return out

    def mark_nagged(self, group_id, promise_id, now=None):
        now = time.time() if now is None else now
        for r in self.list(group_id):
            if r["id"] == promise_id:
                r["nag"] = int(r.get("nag") or 0) + 1
                r["last_nag"] = now
                if r["nag"] >= self.max_nag:
                    self.items[group_id] = [
                        x for x in self.items[group_id] if x["id"] != promise_id
                    ]
                return r["nag"]
        return 0

    def resolve(self, group_id, keyword="", who=None, now=None):
        """手动结清：按关键词或人名撤下。群友说「我兑现了」时用。"""
        rows = self.list(group_id)
        if not rows:
            return 0
        kw = (keyword or "").strip().lower()
        keep, removed = [], 0
        for r in rows:
            hit = False
            if who and who in str(r.get("who") or ""):
                hit = True
            if kw and (kw in str(r.get("what") or "").lower()
                       or kw in str(r.get("who") or "").lower()):
                hit = True
            if hit:
                removed += 1
            else:
                keep.append(r)
        self.items[group_id] = keep
        return removed

    def render(self, group_id, title="还没兑现"):
        """0 token 渲染，用于「查账」命令。"""
        rows = self.list(group_id)
        if not rows:
            return "🧾 【承诺台账】目前是干净的——要么大伙言出必行，要么还没人敢在我面前画饼。"
        out = [f"🧾 【承诺台账】共 {len(rows)} 条："]
        for r in rows:
            due = r.get("due_text") or "没说时间"
            nag = f"，已催 {r['nag']} 次" if r.get("nag") else ""
            out.append(f"  • {r.get('who','有人')}：{r.get('what','')}（{due}{nag}）")
        return "\n".join(out)
