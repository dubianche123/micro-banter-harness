"""运行时单例：跨重启档案对象的唯一组装点（2026-10-06 从 bot.py 拆出）。

这里的构造只依赖 config 和各自领域模块，**不回指 bot** —— bot.py 与各领域模块
（providers / affinity / nicking）都从这里拿单例，依赖图保持无环。
⚠️ OWNER_OPENID 会被 load_owner 重新绑定：别的模块要读它必须写 `runtime.OWNER_OPENID`，
不许 `from runtime import OWNER_OPENID`（那会在 import 时把 None 钉死成常量）。
"""
import os

import archive as archive_mod
import config
import digest as digest_mod
import naming
import relations
import storage
import logging
logger = logging.getLogger("qqbot")   # 与 bot.py 同一个命名 logger，共享 handler

# ══════════════════════ 3. 持久化状态 ══════════════════════

STATE = storage.StateStore(config.STATE_FILE, config.STATE_SAVE_INTERVAL)
SESSIONS = storage.SessionStore(
    max_turns=config.CONTEXT_MAX_TURNS,
    max_chars=config.CONTEXT_MAX_CHARS,
    ttl=config.CONTEXT_TTL_SECONDS,
)
SESSIONS.hydrate(STATE.data.get("sessions", {}))
STATE.register_collector(SESSIONS.dump_into)

# 群友关系档案：跨重启记住「跟谁熟」。分工是——
#   分数、分级、衰减由代码负责；模型只负责判断一次互动的情感倾向 + 把分数说成人话。
RELATIONS = relations.RelationStore(
    grace_days=config.AFFINITY_DECAY_GRACE_DAYS,
    step_days=config.AFFINITY_DECAY_STEP_DAYS,
    amount=config.AFFINITY_DECAY_AMOUNT,
    prune_idle_days=config.AFFINITY_PRUNE_IDLE_DAYS,
)
RELATIONS.hydrate(STATE.data.get("relations", {}))
STATE.register_collector(RELATIONS.dump_into)

# 旧称呼台账：改名/撤销称呼时登记「这个名字曾经属于谁」，注入 prompt 前用它把历史文本里的
# 旧字面改写成当前称呼。**它本身不进模型上下文** —— 模型看到的永远只有最新那一版名字。
RENAMES = relations.RenameLedger()
RENAMES.hydrate(STATE.data.get("renames", {}))
STATE.register_collector(RENAMES.dump_into)

# 群里平台侧的显示名（QQ 群昵称）。只在正文里有明文 @ 时才学得到，
# 纯显示兜底 —— 认领过的称呼永远优先，它不参与任何判断，也不进模型上下文。
DISPLAY_NAMES = relations.DisplayNames()
DISPLAY_NAMES.hydrate(STATE.data.get("display_names", {}))
STATE.register_collector(DISPLAY_NAMES.dump_into)

# 原始消息归档：所有收到的消息按天存 JSONL。摘要是有损的，出错了要能回来查原文。
ARCHIVE = archive_mod.MessageArchive(
    base_dir=config.BASE_DIR,
    archive_dir=config.ARCHIVE_DIR,
    memory_dir=config.MEMORY_DIR,
    keep_days=config.ARCHIVE_KEEP_DAYS,
    max_text=config.ARCHIVE_MAX_TEXT,
)

# 群聊长期记忆：每天把当天的新聊天滚动压缩进一份摘要，开销恒定，不随群活跃度膨胀。
# 待压缩的数据来自 ARCHIVE（按 last_run 水位线切分），这里只存摘要本身。
DIGESTS = digest_mod.GroupDigest(
    chunk_chars=config.DIGEST_CHUNK_CHARS,
    max_blocks=config.DIGEST_MAX_BLOCKS,
    max_entries=config.DIGEST_MAX_PENDING,
)
DIGESTS.hydrate(STATE.data.get("digests", {}))
STATE.register_collector(DIGESTS.dump_into)
_digest_locks = {}

# 承诺账本：群里立下却没兑现的话，到期就催
PROMISES = digest_mod.PromiseBook(
    grace_hours=config.PROMISE_GRACE_HOURS,
    nag_interval_hours=config.PROMISE_NAG_INTERVAL_HOURS,
    max_nag=config.PROMISE_MAX_NAG,
    max_items=config.PROMISE_MAX_ITEMS,
)
PROMISES.hydrate(STATE.data.get("promises", {}),
                 finished=STATE.data.get("promises_finished", {}))
STATE.register_collector(PROMISES.dump_into)

# 群级令牌桶 + 全局日预算。按需求刻意【不做】per-user 额度限制。
GROUP_BUCKET = storage.TokenBucket(config.GROUP_RATE_CAPACITY, config.GROUP_RATE_REFILL_SECONDS)
BUDGET = storage.DailyBudget(config.DAILY_BUDGET, STATE.data.setdefault("usage", {}))

OWNER_OPENID = None

def load_owner():
    global OWNER_OPENID
    if os.path.exists(config.OWNER_FILE):
        try:
            with open(config.OWNER_FILE, encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                OWNER_OPENID = saved
                # 群主的好感度钉死在最高档：亲近靠档案体现，不靠谄媚台词
                RELATIONS.pin(OWNER_OPENID)
                logger.info("👑 已加载持久化群主 OpenID: %s（好感度已锁定顶格）", OWNER_OPENID)
        except Exception as e:
            logger.warning("读取 owner.txt 失败: %s", e)

def owner_label(group_id=None):
    """群里怎么称呼群主：他自己认领的称呼；还没认领就退回通用词「群主」。

    刻意**不写死任何人名**：换个群、群主改个名，这套东西得开箱能用。
    """
    if not OWNER_OPENID or not group_id:
        return naming.DEFAULT_OWNER_LABEL
    rec = RELATIONS.get(group_id, OWNER_OPENID, create=False) or {}
    return naming.owner_label(rec.get("nick"))

def save_owner(openid):
    try:
        with open(config.OWNER_FILE, "w", encoding="utf-8") as f:
            f.write(openid)
    except Exception as e:
        logger.warning("写入 owner.txt 失败: %s", e)

def hits(text, words):
    """本地触发词匹配的**唯一入口**：子串匹配、空词安全。

    以前每个判断点都手写一遍 `any(k in text for k in words)`，十一处轮子；
    收敛到这里之后，匹配规则要改（比如加词边界、做归一化）只改这一处。
    """
    t = text or ""
    return any(w and w in t for w in words)


def _all_named(group_id):
    """本群所有留过称呼的人 [(openid, rec)]，最近的排前面。"""
    prefix = f"{group_id}|"
    rows = [(k[len(prefix):], r) for k, r in RELATIONS.records.items()
            if k.startswith(prefix) and r.get("nick")]
    rows.sort(key=lambda kv: kv[1].get("last_seen", 0.0), reverse=True)
    return rows
