"""可编辑知识库 —— 维护者直接改 Markdown，按关键词命中后进本轮动态层。

为什么单独做一层
----------------
摘要和名册是模型从聊天里归纳出来的，记错了只能等下一轮压缩慢慢纠；
有些事实（谁是谁、某个梗的由来、群规、固定安排）本来就不该靠模型猜。
这里给维护者一个直接改的入口：改文件、保存、下一轮生效，不需要重启或重新压缩。

文件约定
--------
    knowledge/all.md          所有群共享
    knowledge/<group_id>.md   只对某个群生效，同名条目覆盖 all.md

每个二级标题是一条，第一行可写：
    ## 阿强请奶茶
    关键词：奶茶、请客、答应
    阿强答应请全群喝奶茶，具体时间还没定。

没有「关键词」行时，标题本身当关键词。命中后只取少量条目放进本轮最末，
不常驻、不每轮全量注入；没有命中就当它不存在。
"""

import logging
import os
import re

import config
import wordfilter

logger = logging.getLogger("qqbot")

KNOWLEDGE_MARK = "【群知识库"
HEADER = f"""{KNOWLEDGE_MARK}（维护者直接维护的事实；和摘要冲突时以此为准）】
只在当前问题确实相关时使用；不要主动扩展、不要复述整条，也不要说“知识库”。】"""

SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
KEYWORDS_RE = re.compile(r"^\s*(?:关键词|关键字|keywords?)\s*[:：]\s*(.+?)\s*$", re.I)
SPLIT_RE = re.compile(r"[,，、;；/|\s]+")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
MACHINE_ID = re.compile(r"<@!?[0-9A-Fa-f]{16,}>|(?<![0-9A-Fa-f])[0-9A-Fa-f]{16,}(?![0-9A-Fa-f])")


def _keywords(raw):
    out = []
    for item in SPLIT_RE.split(raw or ""):
        item = item.strip()
        if len(item) < 2:
            continue
        if item not in out:
            out.append(item)
        if len(out) >= 8:
            break
    return tuple(out)


class KnowledgeBase:
    """按群缓存解析结果；文件 mtime/size 变了才重读。"""

    def __init__(self, directory, max_cached_groups=100):
        self.directory = directory
        self.max_cached_groups = max_cached_groups
        self._cache = {}
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError as exc:
            logger.warning("知识库目录不可用 %s: %s", self.directory, type(exc).__name__)

    def _paths(self, group_id):
        # 群文件在前：同名条目由群内版本覆盖共享版本。
        return [
            os.path.join(self.directory, f"{group_id}.md"),
            os.path.join(self.directory, "all.md"),
        ]

    @staticmethod
    def _signature(paths):
        out = []
        for path in paths:
            try:
                stat = os.stat(path)
                out.append((path, stat.st_mtime_ns, stat.st_size))
            except OSError:
                out.append((path, None, None))
        return tuple(out)

    @staticmethod
    def _parse_file(path, seen):
        try:
            with open(path, encoding="utf-8") as stream:
                text = COMMENT_RE.sub("", stream.read())
        except OSError:
            return []
        matches = list(SECTION_RE.finditer(text))
        entries = []
        for index, match in enumerate(matches):
            title = match.group(1).strip()
            if not title:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end():end]
            keyword_line = None
            content_lines = []
            for line in body.splitlines():
                found = KEYWORDS_RE.match(line)
                if found and keyword_line is None:
                    keyword_line = found.group(1)
                else:
                    content_lines.append(line)
            keywords = _keywords(keyword_line or title)
            content = " ".join(" ".join(content_lines).split())
            content = MACHINE_ID.sub("（群友编号）", content)
            if not keywords or not content:
                continue
            if wordfilter.has_hit(title + " " + content):
                logger.warning("知识库有条目命中敏感词，已跳过（%s）", os.path.basename(path))
                continue
            key = title[:40]
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "title": key,
                "keywords": keywords,
                "content": content[:300],
            })
        return entries

    def parse_file(self, path):
        """给维护面板预览用：只解析指定文件，不合并 all.md。"""
        return self._parse_file(path, set())

    def entries(self, group_id):
        paths = self._paths(group_id)
        signature = self._signature(paths)
        cached = self._cache.get(group_id)
        if cached and cached[0] == signature:
            return cached[1]
        seen = set()
        entries = []
        for path in paths:
            entries.extend(self._parse_file(path, seen))
        self._cache[group_id] = (signature, entries)
        while len(self._cache) > self.max_cached_groups:
            self._cache.pop(next(iter(self._cache)))
        return entries

    def match(self, group_id, text, limit=None):
        text = (text or "").strip()
        if not text or not config.KNOWLEDGE_ENABLED or wordfilter.has_hit(text):
            return []
        limit = config.KNOWLEDGE_MAX_ITEMS if limit is None else int(limit)
        if limit <= 0:
            return []
        lowered = text.lower()
        scored = []
        for entry in self.entries(group_id):
            hits = [kw for kw in entry["keywords"] if kw.lower() in lowered]
            if hits:
                scored.append((max(len(kw) for kw in hits), entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _score, entry in scored[:limit]]

    def render(self, group_id, text):
        entries = self.match(group_id, text)
        if not entries:
            return ""
        budget = max(60, config.KNOWLEDGE_MAX_CHARS)
        lines = []
        for entry in entries:
            prefix = f"- {entry['title']}："
            room = budget - len(prefix)
            if room < 40:
                break
            content = entry["content"]
            if len(content) > room:
                content = content[:room - 1] + "…"
            lines.append(prefix + content)
            budget -= len(lines[-1])
            if budget <= 0:
                break
        if not lines:
            return ""
        return HEADER.strip() + "\n" + "\n".join(lines)

    def count(self):
        """给状态/诊断用：当前目录里共有多少条可注入条目。"""
        total = 0
        try:
            names = sorted(os.listdir(self.directory))
        except OSError:
            return 0
        seen = set()
        for name in names:
            if not name.endswith(".md") or name == "README.md":
                continue
            total += len(self._parse_file(os.path.join(self.directory, name), seen))
        return total
