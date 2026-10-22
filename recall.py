"""按需旧账召回 —— 只在对方明确问起过去时，从本地归档取 1~2 条。

为什么不每轮注入：2026-10-06 的对照实验里，只要旧事材料在眼前，模型就会长出
「回忆尾巴」——哪怕没人问。现在长期记忆只喂人物名册，旧事由「翻旧账」命令按需查。
这里补的是同一思路的自动入口：高精度触发、只读本地归档（0 token）、只取少量，
并放进本轮最末的动态层，绝不碰稳定头和每日记忆。

触发条件刻意收紧：必须出现「上次 / 之前 / 答应过 / 还记得」这类往事线索，
并且能从中提取出人名或具体内容词。只有线索没有内容（「你还记得吗」）不查；
命中敏感词的查询和片段直接放弃 —— 宁可不召回，也不把旧雷翻出来。
"""

import logging
import re
import time

import config
import wordfilter

logger = logging.getLogger("qqbot")

RECALL_MARK = "【旧账检索"
HEADER = f"""{RECALL_MARK}（系统按你的问题从本地归档翻到的旧消息）】
只在对方问的正是这件事时拿它核对；对不上就忽略。不要主动提旧事、不要复述整段，
也不要用「我记得你以前」起头。引用时对准说话人，别把别人的事安到眼前这位头上。】"""

# 强往事线索。刻意不收「什么时候」「查一下」——它们太常出现在与旧事无关的
# 眼前问题里（「你什么时候下班」），会凭空把旧聊天翻出来。
RECALL_CUES = (
    "上次", "上回", "那天", "当时", "那次", "之前", "以前", "后来",
    "还记得", "记不记得", "你说过", "跟我说过", "答应过", "答应", "承诺",
    "不是说好", "说好", "兑现", "翻旧账", "旧账", "谁说的", "哪天", "多久了",
    "来着",
)
PROMISE_CUES = (
    "答应", "承诺", "不是说好", "说好", "兑现", "请客", "请我", "请全群",
    "欠我", "欠大家", "还欠", "欠着",
)

# 先剥掉这些短语再抽内容词，避免「奶茶的事」被切成「奶茶事」而搜不到。
STOP_PHRASES = (
    "那个", "这个", "那些", "这些", "什么", "怎么", "为什么", "哪个", "哪里",
    "事情", "事儿", "东西", "时候", "一下", "一个", "一件", "有没有", "是不是",
    "能不能", "要不要", "可不可以", "帮我", "给我", "告诉我", "知道", "记得",
    "的事", "那件事", "这件事", "什么事",
)
PARTICLES = frozenset("的了是在有和跟与就都也还很太我你他她它谁哪这那")
HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
WORD_RUN = re.compile(r"[A-Za-z0-9]{2,12}")
QUOTED = re.compile(r"[「“\"《]([^」”\"》]{2,12})[」”\"》]")
MACHINE_ID = re.compile(r"<@!?[0-9A-Fa-f]{16,}>|(?<![0-9A-Fa-f])[0-9A-Fa-f]{16,}(?![0-9A-Fa-f])")


def has_cue(text):
    return [cue for cue in RECALL_CUES if cue and cue in (text or "")]


def _strip_phrases(text, phrases):
    for phrase in sorted(phrases, key=len, reverse=True):
        text = text.replace(phrase, " ")
    return text


def extract_terms(text, names=()):
    """从问题里抽内容词；中文取 2~4 字 n-gram，英文/数字取整段。

    人名不放进内容词集合：它在 _score 里单独计分。否则「阿强」本身会同时
    算作名字命中和内容命中，一条只提到名字的短消息会把真正相关的旧账挤掉。
    """
    text = text or ""
    terms = set()
    for match in QUOTED.finditer(text):
        phrase = match.group(1).strip()
        if len(phrase) >= 2:
            terms.add(phrase)

    cleaned = _strip_phrases(text, RECALL_CUES)
    cleaned = _strip_phrases(cleaned, STOP_PHRASES)
    for name in sorted(names, key=len, reverse=True):
        if name:
            cleaned = cleaned.replace(name, " ")
    cleaned = "".join(" " if ch in PARTICLES else ch for ch in cleaned)
    for run in HAN_RUN.findall(cleaned):
        if len(run) < 2:
            continue
        for size in range(2, min(4, len(run)) + 1):
            for start in range(len(run) - size + 1):
                terms.add(run[start:start + size])
    for run in WORD_RUN.findall(cleaned):
        terms.add(run.lower())
    return {term for term in terms if len(term) >= 2}


def _score(row, terms, names, now):
    """命中人名/内容词才计分；短于 4 字、敏感、指令行直接放弃。"""
    text = str(row.get("text") or "").strip()
    if len(text) < 4 or text == "[图片]" or row.get("cmd"):
        return 0
    if wordfilter.has_hit(text):
        return 0
    name_hits = sum(1 for name in names if name and name in text)
    lowered = text.lower()
    term_hits = sum(1 for term in terms if term in lowered)
    if not name_hits and not term_hits:
        return 0
    # 问题里有具体内容词时，只沾了人名、没沾那件事的消息不算证据；
    # 否则「阿强上次说的奶茶」会把同一个人最近的无关发言也带进来。
    if terms and not term_hits:
        return 0
    if term_hits:
        # 具体内容词命中才是主证据；人名只加分，不能盖过「奶茶」本身。
        score = 8 + (2 if name_hits else 0) + min(max(term_hits - 1, 0), 3)
    else:
        # 只有人名时只收较长、像一件事的消息，避免「阿强：草」抢占名额。
        if len(text) < 12:
            return 0
        score = 3 + min(name_hits, 2)
    age_days = max(0.0, (now - float(row.get("ts") or 0)) / 86400.0)
    score += 2 if age_days <= 7 else (1 if age_days <= 14 else 0)
    if len(text) >= 15:
        score += 1
    return score


def _pick(rows, terms, names, now, max_items, exclude_text=""):
    scored = []
    seen_ids = set()
    for row in rows or []:
        text = str(row.get("text") or "").strip()
        if not text or text == exclude_text:
            continue
        message_id = str(row.get("message_id") or "")
        if message_id and message_id in seen_ids:
            continue
        score = _score(row, terms, names, now)
        if not score:
            continue
        if message_id:
            seen_ids.add(message_id)
        scored.append((score, float(row.get("ts") or 0), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    # 优先不同说话人；同一句旧账被一个人连刷几条时别占满名额。
    picked, senders = [], set()
    for _score_value, _ts, row in scored:
        if len(picked) >= max_items:
            break
        sender = str(row.get("sender") or "")
        if sender and sender in senders:
            continue
        picked.append(row)
        senders.add(sender)
    if len(picked) < max_items:
        ids = {str(row.get("message_id") or "") for row in picked}
        for _score_value, _ts, row in scored:
            if len(picked) >= max_items:
                break
            if str(row.get("message_id") or "") not in ids:
                picked.append(row)
    return picked[:max_items]


def _promise_line(promises, group_id, text, terms):
    if promises is None or not any(cue in text for cue in PROMISE_CUES):
        return ""
    try:
        rows = promises.list(group_id)
    except Exception:
        return ""
    best, best_score = None, 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        who = str(row.get("who") or "")
        what = str(row.get("what") or "")
        if not what:
            continue
        score = 0
        if who and who in text:
            score += 3
        if any(term and (term in what or term in who) for term in terms):
            score += 2
        if score > best_score:
            best, best_score = row, score
    if best is None:
        return ""
    due = str(best.get("due_text") or "没说时间")
    return (f"- 承诺：{best.get('who') or '有人'} 说过「{best.get('what')}」"
            f"（{due}）")


def build_note(group_id, text, *, archive, promises=None, names=(), name_of=None,
               now=None, max_items=None, lookback_days=None, exclude_text=""):
    """返回可直接塞进本轮动态层的召回块；没有可靠命中时返回空串。"""
    if not config.MEMORY_RECALL_ENABLED:
        return ""
    text = (text or "").strip()
    if not text or not has_cue(text) or wordfilter.has_hit(text):
        return ""

    names = tuple(name for name in names if name and len(name) >= 2)
    # 词典里的人名只用来切词；真正参与打分的必须是**这条问题里点到的人**，
    # 否则「你还记得吗」也会因为旧账里出现任意熟人而被召回。
    query_names = tuple(name for name in names if name in text)
    terms = extract_terms(text, names)
    if not terms and not query_names:
        return ""
    now = time.time() if now is None else now
    max_items = config.MEMORY_RECALL_MAX_ITEMS if max_items is None else max_items
    lookback = (config.MEMORY_RECALL_LOOKBACK_DAYS if lookback_days is None
                else lookback_days)
    if max_items <= 0:
        return ""

    try:
        rows = archive.search(group_id, "", since_ts=now - max(1, lookback) * 86400.0,
                              limit=3000)
    except Exception as exc:
        logger.warning("旧账召回读取归档失败: %s", type(exc).__name__)
        return ""

    lines = []
    promise = _promise_line(promises, group_id, text, terms)
    if promise:
        lines.append(promise)
        max_items -= 1
    for row in _pick(rows, terms, query_names, now, max_items,
                     exclude_text=exclude_text):
        body = " ".join(str(row.get("text") or "").split())
        body = MACHINE_ID.sub("（群友编号）", body)
        if not body:
            continue
        if len(body) > 80:
            body = body[:80] + "…"
        sender = str(row.get("sender") or "")
        who = (name_of(sender) if name_of else None) or "一位群友"
        when = time.strftime("%m-%d %H:%M",
                             time.localtime(float(row.get("ts") or now)))
        lines.append(f"- {when} {who}：{body}")
    if not lines:
        return ""
    return HEADER.strip() + "\n" + "\n".join(lines)
