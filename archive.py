"""原始消息归档 + 每日 Markdown 产出。

为什么要单独存一份原文
----------------------
压缩摘要是有损的：模型会丢掉它认为不重要的东西，偶尔还会张冠李戴（压测里出现过把
阿强说的话记到老李头上）。如果原文不留存，摘要一旦出错就无从核对，只能将错就错。

所以这里把两件事分开：
    archive/YYYY-MM-DD.jsonl   原始消息，一行一条，只增不改，是唯一的真相来源
    memory/YYYY-MM-DD.md       每天压缩出来的摘要，人看这个就够

绝大部分时候只看 MD；真要查证，回到 JSONL 按时间/人/关键词搜就行。

这也顺带解决了另一个问题：摘要的「待压缩队列」不用再塞进 state.json 了。
以前 pending 既占内存又占状态文件，现在待压缩 = 归档里已提交偏移之后的那些行。
偏移随摘要保存在 StateStore，只有成功处理的前缀才能提交；同毫秒与晚到消息也不会被跳过。
"""

import json
import os
import re
import time
import uuid

DEFAULT_ARCHIVE_DIR = "archive"
DEFAULT_MEMORY_DIR = "memory"
DATE_FMT = "%Y-%m-%d"


def day_key(ts=None):
    return time.strftime(DATE_FMT, time.localtime(ts if ts is not None else time.time()))


def _safe_tail(text, n=8):
    """群 openid 可能很长，用来做文件名/短标识时截一段就够了。"""
    return re.sub(r"[^A-Za-z0-9]", "", str(text or ""))[-n:] or "unknown"


class MessageArchive:
    """按天分文件的 JSONL 归档。写入即 flush，进程被强杀也只丢最后一行。"""

    def __init__(self, base_dir, archive_dir=DEFAULT_ARCHIVE_DIR,
                 memory_dir=DEFAULT_MEMORY_DIR, keep_days=90, max_text=500):
        self.base_dir = base_dir
        self.archive_dir = os.path.join(base_dir, archive_dir)
        self.memory_dir = os.path.join(base_dir, memory_dir)
        self.keep_days = int(keep_days)
        self.max_text = int(max_text)
        self._fp = None
        self._fp_day = None
        self._last_prune = 0.0
        self._message_cache = {}
        os.makedirs(self.archive_dir, exist_ok=True)
        os.makedirs(self.memory_dir, exist_ok=True)

    # ── 写入 ──
    def _file_for(self, day):
        return os.path.join(self.archive_dir, f"{day}.jsonl")

    def _ensure_fp(self, day):
        if self._fp is None or self._fp_day != day:
            self.close()
            self._fp = open(self._file_for(day), "a", encoding="utf-8")
            self._fp_day = day
        return self._fp

    def append(self, group_id, sender, text, ts=None, at=False, keep=True, cmd=False,
               message_id=None):
        """落一条原始消息。返回写入的字典（被过滤掉则返回 None）。

        cmd=True 表示这是一条**本地控制指令**（「猫娘模式」「查好感」「决斗」之类），
        不是聊天内容。它照样落盘留痕，但压缩长期记忆时会跳过（见 since）——
        否则「喊猫娘模式」会被当成「这人想变猫娘」写进人物档案，实测已经发生过。
        """
        ts = time.time() if ts is None else ts
        t = (text or "").strip()
        if not t:
            return None
        row = {
            "ts": round(ts, 3),
            "day": day_key(ts),
            "group": group_id,
            "sender": sender,
            "text": t[: self.max_text],
            "message_id": str(message_id or uuid.uuid4().hex),
        }
        if at:
            row["at"] = 1
        if cmd:
            row["cmd"] = 1
        self._write_row(row)
        self._message_cache[row["message_id"]] = row
        if len(self._message_cache) > 5000:
            self._message_cache.pop(next(iter(self._message_cache)))
        if keep:
            self._maybe_prune(ts)
        return row

    def _write_row(self, row):
        fp = self._ensure_fp(row["day"])
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        fp.flush()

    def _source_message(self, group_id, message_id):
        cached = self._message_cache.get(message_id)
        if cached and cached.get("group") == group_id:
            return cached
        for name in self._archive_names():
            try:
                with open(os.path.join(self.archive_dir, name), encoding="utf-8") as fp:
                    for line in fp:
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if (isinstance(row, dict) and row.get("group") == group_id
                                and row.get("message_id") == message_id
                                and row.get("kind") != "caption"):
                            return row
            except OSError:
                continue
        return None

    def append_caption(self, group_id, message_id, caption, sender=None, ts=None):
        """Persist derived image evidence linked to the immutable source ID.

        The original JSONL row is never edited. A caption arriving after a digest
        commit is a new pending event, so it is not lost behind that commit.
        Unknown IDs and mismatched senders are rejected rather than guessed.
        """
        caption = str(caption or "").strip()
        message_id = str(message_id or "")
        source = self._source_message(group_id, message_id)
        if not caption or not source or (sender is not None and sender != source.get("sender")):
            return None
        text = f"[图片：{caption[:self.max_text]}]"
        cache_key = (group_id, message_id, text)
        if cache_key in self._message_cache:
            return self._message_cache[cache_key]
        # Reuse the archive as the durable evidence index. The small shared
        # in-process cache only avoids rereading it for concurrent references.
        for name in self._archive_names():
            try:
                with open(os.path.join(self.archive_dir, name), encoding="utf-8") as fp:
                    for line in fp:
                        try:
                            existing = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if (isinstance(existing, dict) and existing.get("group") == group_id
                                and existing.get("kind") == "caption"
                                and existing.get("message_id") == message_id
                                and existing.get("text") == text):
                            self._message_cache[cache_key] = existing
                            return existing
            except OSError:
                continue
        ts = time.time() if ts is None else ts
        row = {
            "ts": round(ts, 3), "day": day_key(ts), "group": group_id,
            "sender": source.get("sender"), "kind": "caption",
            "message_id": source["message_id"], "evidence_id": uuid.uuid4().hex,
            "text": text,
        }
        self._write_row(row)
        self._message_cache[cache_key] = row
        if len(self._message_cache) > 5000:
            self._message_cache.pop(next(iter(self._message_cache)))
        return row

    def close(self):
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None
            self._fp_day = None

    def _maybe_prune(self, now):
        """每天只清理一次，别每条消息都去扫目录。"""
        if self.keep_days <= 0 or now - self._last_prune < 86400:
            return
        self._last_prune = now
        self.prune(now)

    def prune(self, now=None):
        """删掉超过保留期的归档文件。返回删除数量。"""
        now = time.time() if now is None else now
        if self.keep_days <= 0:
            return 0
        cutoff = day_key(now - self.keep_days * 86400.0)
        removed = 0
        try:
            for name in os.listdir(self.archive_dir):
                if not name.endswith(".jsonl"):
                    continue
                if name[:-6] < cutoff:
                    try:
                        os.unlink(os.path.join(self.archive_dir, name))
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    # ── 读取 ──
    def _days_between(self, since_ts, until_ts=None):
        """列出要扫描的日期文件。

        注意起点必须是 max(since_ts, 一年前)：新群的 last_run 是 0（1970 年），
        要是不夹一下，这里的循环会先跑满 366 次上限全花在 1970 年上，
        结果新群一条消息都读不到、永远不会被压缩。
        """
        until_ts = time.time() if until_ts is None else until_ts
        start = max(float(since_ts or 0.0), until_ts - 365 * 86400.0)
        days, cur = [], start
        for _ in range(400):
            d = day_key(cur)
            if d not in days:
                days.append(d)
            if d >= day_key(until_ts):
                break
            cur += 86400.0
        return days

    def _archive_names(self):
        try:
            return sorted(name for name in os.listdir(self.archive_dir)
                          if name.endswith(".jsonl"))
        except OSError:
            return []

    def pending(self, group_id, cursor=None, since_ts=0, limit=5000,
                include_cmd=False):
        """Return the oldest uncommitted entries and their read-offset snapshot.

        Offsets are byte positions in append-only daily files. Capture file sizes
        before reading: messages arriving during model calls (even with equal or
        older event timestamps) remain beyond the committed snapshot. Each entry
        carries its prefix `_cursor`, allowing bounded digest blocks to commit
        exactly the portion they processed. This metadata is never rendered.

        A missing cursor imports old timestamp-only state. Its lower bound is
        carried with the initial file-size snapshot until all of that snapshot
        has been scanned, including filtered tails after a returned prefix.
        Later arrivals are outside that snapshot and are never timestamp-filtered.
        """
        offsets = dict(cursor or {})
        files = []
        for name in self._archive_names():
            path = os.path.join(self.archive_dir, name)
            try:
                files.append((name, path, os.path.getsize(path)))
            except OSError:
                continue
        migration = offsets.get("_legacy")
        if cursor is None and since_ts:
            migration = {"since_ts": float(since_ts),
                         "ends": {name: end for name, _, end in files}}
            offsets["_legacy"] = migration
        out = []
        for name, path, end in files:
            pos = max(0, int(offsets.get(name) or 0))
            try:
                with open(path, "rb") as fp:
                    fp.seek(pos)
                    while fp.tell() < end:
                        line = fp.readline(end - fp.tell())
                        if not line or not line.endswith(b"\n"):
                            break   # a partial tail is retried, not committed
                        offsets[name] = fp.tell()
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(row, dict) or row.get("group") != group_id:
                            continue
                        if (migration and fp.tell() <= migration["ends"].get(name, 0)
                                and float(row.get("ts") or 0) <= migration["since_ts"]):
                            continue
                        if not include_cmd and row.get("cmd"):
                            continue
                        row["_cursor"] = dict(offsets)
                        out.append(row)
                        if limit and len(out) >= limit:
                            return out, dict(offsets)
            except OSError:
                continue
        # A skipped legacy tail belongs to the same scan, even when the last
        # returned message was earlier in the file. Commit it only with that
        # final message; bounded earlier prefixes retain migration metadata.
        if migration and all(offsets.get(name, 0) >= migration["ends"].get(name, 0)
                             for name, _, _ in files):
            offsets.pop("_legacy", None)
        if out:
            out[-1]["_cursor"] = dict(offsets)
        return out, dict(offsets)

    def since(self, group_id, since_ts, limit=5000, include_cmd=False):
        """Timestamp-based historical read; new digest work should use pending().

        Retained for callers and old state migration. Limit keeps the oldest
        prefix, so a bounded consumer can never skip unprocessed earlier rows.
        """
        rows, _ = self.pending(group_id, since_ts=since_ts, limit=limit,
                               include_cmd=include_cmd)
        for row in rows:
            row.pop("_cursor", None)
        return rows

    def search(self, group_id=None, keyword="", since_ts=None, limit=50):
        """出错了回去查原文：按关键词翻旧账。"""
        kw = (keyword or "").strip().lower()
        hits = []
        for name in sorted(os.listdir(self.archive_dir), reverse=True):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.archive_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if group_id and row.get("group") != group_id:
                            continue
                        if since_ts and float(row.get("ts") or 0) < since_ts:
                            continue
                        if kw and kw not in str(row.get("text") or "").lower():
                            continue
                        hits.append(row)
                        if len(hits) >= limit:
                            return hits
            except OSError:
                continue
        return hits

    def stats(self):
        try:
            files = [f for f in os.listdir(self.archive_dir) if f.endswith(".jsonl")]
        except OSError:
            return {"days": 0, "files": []}
        total = 0
        for name in files:
            try:
                with open(os.path.join(self.archive_dir, name), encoding="utf-8") as f:
                    total += sum(1 for _ in f)
            except OSError:
                pass
        return {"days": len(files), "lines": total, "files": sorted(files)[-3:]}

    # ── Markdown 产出 ──
    def memory_path(self, group_id, day=None):
        day = day or day_key()
        return os.path.join(self.memory_dir, f"{day}_{_safe_tail(group_id)}.md")

    def write_memory(self, group_id, brief, data, day=None, meta=None):
        """把一次压缩的结果写成当天的 MD。同一天多次压缩会覆盖（以最新为准）。

        同时写一份 latest_{群短号}.md，方便直接打开看最新一版。
        """
        day = day or day_key()
        path = self.memory_path(group_id, day)
        lines = [f"# 群史记 · {day}", ""]
        lines.append(f"> 群 {_safe_tail(group_id)} ｜ 由模型对当天原始消息压缩生成，"
                     f"原文见 `archive/{day}.jsonl`")
        if meta:
            for k, v in meta.items():
                lines.append(f"> {k}：{v}")
        lines.append("")
        if brief:
            lines.append("## 摘要")
            lines.append(brief.strip())
            lines.append("")

        data = data or {}
        if data.get("topics"):
            lines.append("## 最近在聊")
            for t in data["topics"]:
                who = "/".join(t.get("who") or []) if isinstance(t.get("who"), list) else (t.get("who") or "")
                heat = t.get("heat") or ""
                lines.append(f"- {t.get('topic','')}" + (f"（{who}）" if who else "")
                             + (f" · 热度 {heat}" if heat != "" else ""))
            lines.append("")
        if data.get("promises"):
            lines.append("## 还没兑现")
            for p in data["promises"]:
                due = p.get("due") or "未说时间"
                lines.append(f"- **{p.get('who','有人')}**：{p.get('what','')}（说的时间：{due}）")
            lines.append("")
        if data.get("memes"):
            lines.append("## 名场面")
            for m in data["memes"]:
                lines.append(f"- 「{m.get('quote','')}」" + (f" —— {m.get('why','')}" if m.get("why") else ""))
            lines.append("")
        if data.get("people"):
            lines.append("## 人物近况")
            for p in data["people"]:
                lines.append(f"- {p.get('who','')}：{p.get('note','')}")
            lines.append("")

        body = "\n".join(lines).rstrip() + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        latest = os.path.join(self.memory_dir, f"latest_{_safe_tail(group_id)}.md")
        with open(latest, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def rename_in_memory_files(self, group_id, old, new):
        """把本群所有「每日 MD」里的旧称呼改掉，返回改动的文件数。

        这些 MD 是「群史记」命令直接发到群里的正文，留着旧名同样会让人一头雾水。
        原始归档（archive/*.jsonl）是查证底稿，一个字都不动。
        """
        old = (old or "").strip()
        new = (new or "").strip()
        if len(old) < 2 or not new or old == new:
            return 0
        tail = _safe_tail(group_id)
        changed = 0
        try:
            names = os.listdir(self.memory_dir)
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".md") or tail not in name:
                continue
            path = os.path.join(self.memory_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    body = f.read()
                if old not in body:
                    continue
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body.replace(old, new))
                changed += 1
            except OSError:
                continue
        return changed

    def read_memory(self, group_id, day=None):
        path = self.memory_path(group_id, day)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def recent_days(self, group_id, days=7):
        """列出最近若干天的 MD 文件（存在的才列）。"""
        out = []
        for i in range(days):
            d = day_key(time.time() - i * 86400.0)
            p = self.memory_path(group_id, d)
            if os.path.exists(p):
                out.append((d, p))
        return out
