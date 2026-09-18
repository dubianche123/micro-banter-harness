"""QQ 群聊机器人 —— 业务逻辑入口。

配置见 config.py，提示词见 prompts.py，名字解析见 naming.py，状态/上下文/限流见 storage.py。
本文件只负责：接消息 → 判定要不要回 → 走本地彩蛋还是调模型 → 安全发出去。
"""

import asyncio
import collections
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timezone

import aiohttp
import certifi
import httpx
import botpy
from botpy.connection import ConnectionState
from botpy.message import GroupMessage, C2CMessage, Message
from openai import AsyncOpenAI

import archive as archive_mod
import config
import digest as digest_mod
import naming
import prompts
import qqtext
import relations
import storage
import wordfilter

logger = logging.getLogger("qqbot")

# ══════════════════════ 1. 证书 / SSL / 日志 ══════════════════════

os.environ["SSL_CERT_FILE"] = certifi.where()


def setup_logging():
    """带时间戳 + 自动轮转的日志。改造前 bot.log 会无限增长且每行都没有时间信息。"""
    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)


def patch_aiohttp_ssl():
    """botpy 的 WS 连接在某些本机证书链环境下会握手失败，必要时要关掉校验。

    默认是关闭的（与改造前行为一致），但这是有代价的：任何中间人都能看到明文流量。
    本机证书正常的话，请在 .env 里设 SSL_VERIFY=true。
    """
    if config.SSL_VERIFY:
        logger.info("🔒 SSL 校验已开启（SSL_VERIFY=true）")
        return
    logger.warning("🔓 SSL 校验已关闭（本机证书兼容模式），建议本机证书正常时设 SSL_VERIFY=true")

    original_init = aiohttp.TCPConnector.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.pop("verify_ssl", None)
        kwargs.pop("ssl_context", None)
        kwargs.pop("fingerprint", None)
        kwargs["ssl"] = False
        original_init(self, *args, **kwargs)

    aiohttp.TCPConnector.__init__ = patched_init


# botpy 官方 SDK 缺 group_message_create（全量群消息）解析器，这里补上
def parse_group_message_create(self, payload):
    _message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
    self._dispatch("group_message_create", _message)


ConnectionState.parse_group_message_create = parse_group_message_create

# ══════════════════════ 2. AI 客户端 ══════════════════════

_llm_http = httpx.AsyncClient(verify=config.SSL_VERIFY)
ai_clients = [
    AsyncOpenAI(api_key=k, base_url=config.PROVIDER["base_url"], http_client=_llm_http)
    for k in config.AI_KEYS
]
client_idx = 0
CANDIDATE_MODELS = list(config.CANDIDATE_MODELS)

# 供应商梯队：主供应商打不通时依次回落。每家各自持有一组 client（对应各自的 Key 池）。
PROVIDER_CHAIN = [config.AI_PROVIDER]
if config.FALLBACK_PROVIDER and config.FALLBACK_PROVIDER not in PROVIDER_CHAIN:
    PROVIDER_CHAIN.append(config.FALLBACK_PROVIDER)

AI_CLIENTS = {}
MODEL_CHAINS = {}         # 供应商 -> 对话梯队（要好）
MODEL_CHAINS_DIGEST = {}  # 供应商 -> 压缩/批处理梯队（要便宜、要能吞原始聊天）
MODEL_CHAINS_JUDGE = {}   # 供应商 -> 称呼审核梯队（要认得出谐音、拆字、暗指）
for _name in PROVIDER_CHAIN:
    _keys = config.load_api_keys(_name)
    if not _keys:
        continue
    AI_CLIENTS[_name] = [
        AsyncOpenAI(api_key=k, base_url=config.PROVIDER_PRESETS[_name]["base_url"],
                    http_client=_llm_http)
        for k in _keys
    ]
    _preset = config.PROVIDER_PRESETS[_name]
    MODEL_CHAINS[_name] = list(_preset["models"])
    # 压缩梯队刻意**不追加**对话梯队：两个都挂了就等下一轮，消息留在归档里不会丢
    # （run_digest 失败时不会推进 last_run）。宁可晚点压，也不要拿敏感 transcript 去撞强模型的过滤器。
    MODEL_CHAINS_DIGEST[_name] = (list(_preset.get("models_digest") or [])
                                  or list(_preset["models"]))
    # 审核梯队同理独立：这活只有强档干得了，用弱档等于没开（实测 4.5-air 会漏谐音、还误杀正常昵称）
    MODEL_CHAINS_JUDGE[_name] = (list(_preset.get("models_judge") or [])
                                 or list(_preset["models"]))

# tier 名 -> 梯队。加新梯队只要在这里登记一行，call_model 不用动。
_TIER_CHAINS = {
    "chat": MODEL_CHAINS,
    "digest": MODEL_CHAINS_DIGEST,
    "judge": MODEL_CHAINS_JUDGE,
}

if len(PROVIDER_CHAIN) > 1 and len(AI_CLIENTS) > 1:
    print(f"🔀 供应商梯队：{' → '.join(config.PROVIDER_PRESETS[n]['label'] for n in PROVIDER_CHAIN)}")

# 供应商熔断：连续失败就暂时拉黑，免得代理断了之后每条消息都干等一个超时周期
_provider_state = {}   # name -> {"fails": int, "cooldown_until": float}
_key_idx = {}          # name -> 轮到第几个 Key
# 单模型「慢」处罚：(供应商, 模型名) -> 到期时间戳。超时过的一档在这个时间前会被跳过，
# 免得它占着队首让每一轮都先白等一个满超时 —— 见 _penalize_slow_model。
_slow_until = {}
# 最近一条群消息的时刻。它决定「被让位的供应商什么时候能回来」—— 见 _provider_available。
# 内存态即可：重启后当作「群刚静下来」，立刻重试一次主供应商，代价只有一次 prefill。
_activity = {"ts": 0.0}
# 不可恢复错误：连接层（代理断、DNS 挂）+ 地区/权限类。它们跟配额错误不同 ——
# 换模型、换 Key 都救不回来，必须立刻熔断换供应商，
# 否则几个模型逐个试一遍，用户要白等十几秒。
#
# ⚠️ 判定时用的是 f"{异常类名}: {消息}" 并统一转小写，不是光看 str(e)。因为连接层错误的
#    字符串往往只有一句 "Connection error."，类名反而只存在于 type(e).__name__ 里 ——
#    而「代理挂了」正是靠 APIConnectionError 才认得出来（实测 str(e) 里不含任何标记串）。
#    各家 SDK 的大小写不一致（API key not valid / api_key_invalid），所以标记一律小写。
_HARD_FAIL_MARKS = (
    "connecterror", "proxyerror", "remoteprotocolerror", "apiconnectionerror",
    # 超时：httpx 会抛 ConnectTimeout/ReadTimeout/PoolTimeout，但 SDK 通常把它们
    # 统一包成 APITimeoutError 再抛出，所以这个类名才是实际会撞上的那个。
    "apitimeouterror", "connecttimeout", "readtimeout", "pooltimeout",
    "connection error",
    "connection refused", "upstream connect failed", "connect call failed",
    "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "connection reset", "network is unreachable",
    # 地区/凭证类：Gemini 对某些出口 IP 直接返回 400/403，且短时间内不会变
    "location is not supported", "not supported for the api use",
    "api key not valid", "api_key_invalid", "invalid api key", "permission denied",
)

# 「结构性故障」＝上面的硬失败里，那种**等一分钟也不会好**的子集：地区封禁、Key 失效。
# 它们和抖动的区别不是严重等级，而是**会不会自己恢复** —— 代理抖一下几十秒就回来，
# 而 Gemini 的 400 "User location is not supported" 是出口 IP 决定的，两分钟后再试还是同一个错。
#
# 实测（2026-09-17 02:15 → 09-18 15:45，见日志复盘）：这一类 400 在一天半里撞了 **39 次**，
# 每两分钟冷却一到期就被放回来重撞一轮（每次还会顺着模型梯队连试 3 档），
# 全员熔断时甚至被 `_pick_last_resort` 当成「冷却剩余最短」的那家挑去带伤上阵 ——
# 明知道它结构性不可用还让它出工。所以这类错要单独记一笔长的隔离窗，并且在里面
# 不许它在「带伤上阵」里跟别家抢。
_FATAL_FAIL_MARKS = (
    "location is not supported", "not supported for the api use",
    "api key not valid", "api_key_invalid", "invalid api key", "permission denied",
)

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
PROMISES.hydrate(STATE.data.get("promises", {}))
STATE.register_collector(PROMISES.dump_into)

# 群级令牌桶 + 全局日预算。按需求刻意【不做】per-user 额度限制。
GROUP_BUCKET = storage.TokenBucket(config.GROUP_RATE_CAPACITY, config.GROUP_RATE_REFILL_SECONDS)
BUDGET = storage.DailyBudget(config.DAILY_BUDGET, STATE.data.setdefault("usage", {}))

group_buffers = collections.defaultdict(
    lambda: collections.deque(maxlen=config.GROUP_BUFFER_SIZE)
)
for gid, items in (STATE.data.get("buffers") or {}).items():
    group_buffers[gid] = collections.deque(items, maxlen=config.GROUP_BUFFER_SIZE)

group_last_random_reply = dict(STATE.data.get("cooldowns") or {})
processed_msg_ids = collections.deque(maxlen=config.DEDUP_MAX)

OWNER_OPENID = None

# 主动发言（催债）需要拿到 client 实例，这里存个引用
_bot_ref = {"client": None}

# 「老干部」已改为「风纪委员」。存档里如果还留着旧的 cadre，读写时自动迁移，
# 免得群里某一档模式卡在被删除的枚举值上。
LEGACY_MODE_ALIAS = {"cadre": "discipline"}

MODE_PROMPTS = {
    "normal": prompts.PROMPT_NORMAL,
    "catgirl": prompts.PROMPT_CATGIRL,
    "discipline": prompts.PROMPT_DISCIPLINE,
    "crazy": prompts.PROMPT_CRAZY,
    "fortune": prompts.PROMPT_FORTUNE,
    "driver": prompts.PROMPT_DRIVER,
}

FALLBACK_REPLIES = [
    "（战术后仰揉了揉眼睛）刚才走神在看隔壁单挑Boss呢，你刚才这波信息量太大，再说一遍我听着！",
    "（正在专心摸鱼中）刚才屏幕一闪没看清，哪个好兄弟又在群里发功？搞快点，再说一次听听！",
    "（反手掏出一张无懈可击）这波话题有点东西，我先喝口水冷静一下！有种你再艾特我一次，看我怎么接招！",
    "（摘下耳机假装正经）刚才走神了！你刚说啥，再艾特我一次，这回我全神贯注！",
]

_throttle_notice = {}  # group_id -> last notice timestamp


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


# 群主身份**只在私聊里认**：私聊机器人发一句暗号（`config.OWNER_CLAIM_PHRASE`），
# 第一个说对的 openid 被永久写进 owner.txt。
#
# 为什么不在群里认：群聊是大声公。口令一旦在群里念出来就等于念给所有人听，而且是
# 「谁先喊谁得」—— 随手一个群成员、甚至根本不在群里的人，都能把身份抢走。挪到私聊之后，
# 这一句只有当事人自己和机器人看得到，抢答这条路直接没了。
#
# 开源给别人的时候，这一步最容易被漏掉 —— 漏了就只有「普通群友」，所有群主特权全都不生效，
# 而且**没有任何报错**，看起来像功能坏了。所以引导得摆在明面上。
# ⚠️ 引导语里**不写暗号**：它只负责指路，把口令写出来就又把大声公请回来了。
OWNER_CLAIM_HINT = (
    "（顺带一提：这个群还没人认领群主。认领要在**私聊**里跟我说一句暗号 ——"
    "在群里喊谁都会看见，谁先喊谁得，这事不适合公开办。）")

# 群里出现这些字样 = 有人在试着认领。**只用来指路去私聊，永远不会授予权限。**
# 「我是群主」留着是因为群友的第一反应就是喊这句，得把人接住。
# 刻意**不含**真正的暗号：那会让群聊变成一台确认器（喊对了就有人应声 = 等于验证了口令）。
OWNER_CLAIM_PROBE_WORDS = ("我是群主", "认领群主", "认领一下")

# 私聊收到改名命令时的回复。
#
# 为什么必须在这里挡一句：称呼是**按群**存的（`群号|openid`），而私聊消息里没有群号 ——
# 改了也不知道该改在哪个群。以前的坑是这句话直接喂给模型，被当成闲聊回了个玩笑，
# 当事人完全不知道自己那条命令压根没生效（「叫不动它」的错觉就是这么来的）。
# 所以这里是**明说**：不静默吞掉，也不假装受理。0 token、不扣额度。
NICK_PRIVATE_CHAT_HINT = (
    "（把小本本合上了）称呼这事得回群里办 —— 名字是按群记的，"
    "私聊这儿我看不出你说的是哪个群。回群里 @ 我一句「叫我 XXX」就行，"
    "随时能改，以最新一次为准。")

# 普通群友想给别人起名。同理必须**明说**：解析层按权限把这种意图整个吞掉了
# （allow_other=False），不补一句的话请求会掉进闲聊 —— 机器人顺着接一句
# 「别别别，这辈分乱套了」，群里看着像在商量、甚至像改成了，其实一个字都没存。
NICK_OTHER_DENIED = (
    "（赶紧用手按住了小本本）给别人起外号这事得{owner}点头才行。"
    "你要是自己想改称呼，直接说「叫我 XXX」就好，随时能改。")

# 起名锁上之后，普通群友想改称呼得到的答复。同一条铁律：没办成必须**明说** ——
# 静默吞掉再掉进闲聊，是最糟的一种失败（看着像在商量、甚至像办成了）。
NICK_LOCKED_DENIED = (
    "（把小本本锁进了抽屉）现在群里锁着名字，改称呼这事我先不办。"
    "要落名得{owner}点头 —— 让他 @你 说「叫 XXX」就行。")

# 暗号本身就当敏感词注册掉：万一真有人在群里念出来，摘要出口会把它就地抹掉，
# 不会顺着长期记忆回流进每一轮 prompt。
wordfilter.add_runtime_words([config.OWNER_CLAIM_PHRASE])


def owner_hint_pending(group_id):
    """这次回复要不要捎上「怎么认领群主」的引导。每个群最多捎一次。"""
    if OWNER_OPENID or not group_id:
        return False
    slot = STATE.data.setdefault("groups", {}).setdefault(group_id, {})
    if slot.get("owner_hint"):
        return False
    slot["owner_hint"] = True
    STATE.mark_dirty()
    return True


def claim_redirect_pending(group_id):
    """群里有人试着认领时，要不要正面回一句「去私聊办」。每个群最多回一次。

    单独一个槽位，**不和 owner_hint 共用**：一个是「悄悄捎在回复末尾」，一个是「正面顶一句」，
    两条路都可能先被触发；共用一个标记会让另一条永远不出现。
    """
    if OWNER_OPENID or not group_id:
        return False
    slot = STATE.data.setdefault("groups", {}).setdefault(group_id, {})
    if slot.get("claim_redirect"):
        return False
    slot["claim_redirect"] = True
    STATE.mark_dirty()
    return True


def matches_claim_phrase(text):
    """私聊里说对暗号才算数。暗号没配（留空）就永远不算 —— 那等于关掉私聊认主通道。"""
    phrase = (config.OWNER_CLAIM_PHRASE or "").strip()
    return bool(phrase) and phrase in (text or "")


def looks_like_claim_attempt(text):
    """群里有人在试着认领。只用于指路，**不用于授权**。

    群里宁可判宽一点：认错了不过是多一句引导（0 token），漏掉了当事人会一直找不到入口。
    """
    return any(w in (text or "") for w in OWNER_CLAIM_PROBE_WORDS)


# 对方喊停：他明确表示被你的调侃弄不舒服了。
# 判宽一点 —— 误判的代价只是「这次不开玩笑」，漏判的代价是把人越推越远。
RE_BACK_OFF = re.compile(
    r"别(?:这么|这样|再|老|总)?(?:损|嘲|怼|阴阳|挤兑|挖苦|讽|笑话|埋汰)我?"
    r"|(?:太|有点|有够|很|好)?(?:过分|过火|伤人|难听|扎心|难堪)"
    r"|(?:不要|别)(?:这样|这么)(?:说|讲话|说话|跟我)(?:话|了)?"
    r"|说(?:得)?(?:太|有点|这么)(?:狠|重|过分|难听)"
    r"|(?:听着|听)?(?:不舒服|不高兴|难受|不是滋味)"
    r"|认真点|正经点|我不是开玩笑|来真的"
)
# 刻意**不收**「别闹了」：群里这句太常见（「别闹了，说正事」），收了机器人天天变正经，
# 人设就没了。喊停信号只收那些明确指向「你刚才说话的方式」的表达。


def looks_like_back_off(text):
    """对方是不是在喊停（「别这么损我」「有点过分了」）。

    为什么不能交给模型自觉：它已经在错误方向上跑了几轮，等你下一句提示它，
    中间那几句早就把人怼跑了。实测用户连说两次，它每次都加码 —— 第二次还嘴硬
    「你平时损我也没见手软」。所以这里用正则一眼认出来，直接下死命令。
    """
    return bool(RE_BACK_OFF.search(text or ""))


def save_owner(openid):
    try:
        with open(config.OWNER_FILE, "w", encoding="utf-8") as f:
            f.write(openid)
    except Exception as e:
        logger.warning("写入 owner.txt 失败: %s", e)


# ══════════════════════ 4. AI 调用 ══════════════════════


async def probe_provider():
    """启动时单次探活：只做 TLS 握手 + 校验 Key/模型可用性，全程 0 token 消耗。

    刻意【不调 chat 接口】：/chat/completions 哪怕 max_tokens=5 也是真实计费并占 RPM；
    而 GET /models 免费，同样能达到「建连接池 + 验凭证 + 验模型名」三个目的。
    """
    url = config.PROVIDER["base_url"].rstrip("/") + "/models"
    logger.info("🔌 正在探活 [%s] 连接（0 token 消耗）...", config.PROVIDER["label"])
    try:
        t0 = time.time()
        resp = await _llm_http.get(
            url,
            headers={"Authorization": f"Bearer {config.AI_KEYS[client_idx % len(config.AI_KEYS)]}"},
            timeout=15.0,
        )
        elapsed = time.time() - t0
        if resp.status_code != 200:
            logger.warning("⚠️ 探活返回异常状态码 %s: %s", resp.status_code, resp.text[:120])
            return
        available = {m.get("id") for m in (resp.json().get("data") or []) if isinstance(m, dict)}
        # Gemini 的 id 带 "models/" 前缀，智谱不带，剥掉再比，否则会误报模型不存在
        available |= {i.rsplit("/", 1)[-1] for i in available}
        logger.info(
            "✅ 连接预热完成（耗时 %.2fs，含 TLS 握手）| 平台可用模型 %d 个", elapsed, len(available)
        )
        # /models 只收录付费档（此刻 10 个），flash 档常年不在其中却照样能调 ——
        # 「不在列表」不等于「不可用」，所以这里只能提示、不能断言失败。
        # 真正的可用性判据是调用时的 429/1113，交给 failover 梯队接管。
        missing = [m for m in CANDIDATE_MODELS if available and m not in available]
        if missing:
            logger.info(
                "ℹ️ 配置的模型 %s 未出现在 /models 列表 —— 该列表只收录付费档，flash 档常年不在其中，"
                "不代表不可调用；若确已下线，调用会自动回落到梯队下一档", missing
            )
    except Exception as e:
        logger.warning("⚠️ 探活跳过（不影响运行）: %s", str(e)[:120])


def _provider_available(name, allow_yield=True):
    """这家现在能不能用。两条都满足才行：

    1) 熔断冷却过了 —— 失败之后的最短让位时间；
    2) **群已经静下来够久，缓存大概率凉了**（`allow_yield=False` 时跳过这条，
       只看熔断 —— 全员让位时用它破死锁，见 call_model）。

    第 2 条是「缓存优先」的核心。被让位的那家前缀缓存还热着的时候切回去，等于把
    已经付过钱的 prefill 白扔：换一家就要把整段稳定头 + 历史重新算一遍，而群聊的
    每一条消息都紧挨着上一条，命中一次就够本。所以群里还在聊就一直留在正在服务
    的那家；等群静到超过缓存时效，切换才是免费的，这时候才回去重试。

    例外（很重要）：只有「别家顶得上」时才让位。备用也挂了就必须让主供应商自己上，
    否则一级熔断 + 一级失效 = 整条链全哑，宁可多花点 prefill 也不能不说话。
    """
    st = _provider_state.get(name)
    if not st:
        return True
    now = time.time()
    if now < st.get("cooldown_until", 0.0):
        return False
    if allow_yield and now - _last_msg_ts() < config.PROVIDER_CACHE_WARM_SECONDS:
        if any(p != name and _provider_serving(p) for p in PROVIDER_CHAIN):
            return False
    return True


def _penalize_slow_model(pname, model_name):
    """给「刚超时过」的这一档记一笔，让它在 MODEL_SLOW_PENALTY_SECONDS 内先靠边站。

    不是永久降级：记的是**到期时间戳**，凉够了自己回到原位试运行 —— 答上来就留用，
    再超时就再罚一轮。写的是时间而不是改梯队顺序，是因为顺序变了就回不去了：
    晚高峰变慢的模型，过一小时可能又快又好用，把它永久沉到队尾等于白丢一档质量。
    """
    _slow_until[(pname, model_name)] = time.time() + config.MODEL_SLOW_PENALTY_SECONDS
    logger.warning("🐢 [%s] %s 这一档暂时挂免战牌（%.0f 分钟内先跳过去）",
                   config.PROVIDER_PRESETS[pname]["label"], model_name,
                   config.MODEL_SLOW_PENALTY_SECONDS / 60.0)


def _model_slow(pname, model_name):
    """这一档现在是不是还在「慢」的处罚期里。"""
    return time.time() < _slow_until.get((pname, model_name), 0.0)


def _someone_else_can_serve(name, chains):
    """除这家以外，还有别人能出工吗（不看缓存、只看熔断）。

    用来回答「这一脚我可以踹多重」：有别家兜着时，单个模型超时就该立刻把整家摁下去
    （它后面的模型大概率也是一样的慢，别让用户白等）；**没人兜着时**就得手下留情 ——
    整家拉黑等于这一次彻底没话说，而剩下的便宜模型很可能立刻就答上来了。
    """
    return any(
        p != name and AI_CLIENTS.get(p) and chains.get(p)
        and _provider_available(p, allow_yield=False)
        for p in PROVIDER_CHAIN)


def _pick_last_resort(chains):
    """全员真熔断时挑一家「带伤上阵」—— 冷却剩余最短的那家。

    破锁只解决了「互让」，解决不了「两家都在冷却」。实测事故（2026-09-18 14:18）：
    智谱偶发一次 20s 超时被关 120s，而 Gemini 正卡在地区不可用上，于是整整两分钟
    机器人只会回「刚才走神了」。**这时候沉默比多等一次更糟** —— 让最可能已经恢复
    的那家再试一次，成了就成，不成也只是多等一轮。
    """
    def _still_fatal(p):
        """这家是不是还戴着「结构性出局」的帽子（地区封禁 / Key 失效）。"""
        return time.time() < (_provider_state.get(p) or {}).get("fatal_until", 0.0)

    cands = [p for p in PROVIDER_CHAIN if AI_CLIENTS.get(p) and (chains.get(p))]
    if not cands:
        return None

    # 结构性出局的那家不许跟别家抢「带伤上阵」的机会：它只是恰好冷却剩得短，
    # 而它这轮必挂（实测 14:53：让 [Google Gemini] 带伤上阵 → 立刻又一个
    # "User location is not supported"，白等一轮）。只有别无选择时才轮到它。
    healthy = [p for p in cands if not _still_fatal(p)]
    pool = healthy or cands
    return min(pool,
               key=lambda p: (_provider_state.get(p) or {}).get("cooldown_until", 0.0))


def _provider_serving(name):
    """这家现在顶得上吗：有 client、且不在熔断冷却里。"""
    if not AI_CLIENTS.get(name):
        return False
    st = _provider_state.get(name) or {}
    return time.time() >= st.get("cooldown_until", 0.0)


def _provider_block_reason(name):
    """让位的原因，只用于日志 —— 这两种「不可用」的含义完全不同，别混着报。"""
    st = _provider_state.get(name) or {}
    if time.time() < st.get("cooldown_until", 0.0):
        left = st["cooldown_until"] - time.time()
        # 冷却原因要分开说：这两种不可用**能不能靠等**完全不同 —— 前者等一分钟可能就回来了，
        # 后者（地区封禁/凭证失效）等多久都一样，看到它就别再盼它恢复了。
        if time.time() < st.get("fatal_until", 0.0):
            return f"结构性故障（地区/凭证），隔离中，还剩 {left:.0f}s"
        return f"熔断中，还剩 {left:.0f}s"
    idle = time.time() - _last_msg_ts()
    return f"缓存还热（群 {idle:.0f}s 前还在聊 < {config.PROVIDER_CACHE_WARM_SECONDS:.0f}s），先不切回去"


def _last_msg_ts():
    """最近一条群消息的时刻。缓存还热不热，看的就是它。"""
    return _activity["ts"]


def _note_activity():
    """收到群消息就记一笔。让被让位的供应商一直等到群静下来才回来。"""
    _activity["ts"] = time.time()


def _is_hard_fail(exc):
    """这个异常是不是「换模型、换 Key 都救不回来」的那种。

    必须连异常类名一起看：连接层错误的 str(e) 往往只有一句 "Connection error."，
    关键词一个都不出现，类名只存在于 type(e).__name__ 里 —— 而「代理断了」正是
    靠 APIConnectionError 才认得出来。只看 str(e) 的话，代理一断还要连撞满阈值
    才肯换供应商，用户得白等好几轮。
    """
    return any(k in f"{type(exc).__name__}: {exc}".lower() for k in _HARD_FAIL_MARKS)


def _is_fatal_fail(exc):
    """这类错是不是「等冷却过了也不会好」的那种（地区封禁 / Key 失效）。

    是 `_is_hard_fail` 的子集：hard 说的是「这家整条链路现在不通，直接拉黑」，
    fatal 说的是「它不是在抖，它是出局了」——所以隔离窗要长得多，见 `config
    .PROVIDER_FATAL_COOLDOWN_SECONDS`。判定串同样走「类名 + 消息」并小写，
    跟 `_is_hard_fail` 保持一致。
    """
    return any(k in f"{type(exc).__name__}: {exc}".lower() for k in _FATAL_FAIL_MARKS)


def _note_provider_fail(name, hard=False, fatal=False):
    """记一次失败。hard=连接层错误，直接拉黑，不等够阈值。

    fatal=地区封禁/凭证失效这类「等也不会好」的错：不看失败次数，直接关进长隔离窗，
    并记下 `fatal_until` —— 「冷却剩多久」和「是不是结构性出局」是两件事，
    `_pick_last_resort` 要靠后者判断该不该让它带伤上阵（见上面 14:53 那次教训）。
    """
    st = _provider_state.setdefault(name, {"fails": 0, "cooldown_until": 0.0})
    now = time.time()
    label = config.PROVIDER_PRESETS[name]["label"]
    if fatal:
        st["fails"] = 0
        st["cooldown_until"] = now + config.PROVIDER_FATAL_COOLDOWN_SECONDS
        st["fatal_until"] = st["cooldown_until"]
        logger.warning("🚫 供应商 [%s] 结构性故障（地区封禁或凭证失效），隔离 %.0f 分钟 "
                       "—— 这类错不会因为等多久而好转，别每两分钟回来重撞一次",
                       label, config.PROVIDER_FATAL_COOLDOWN_SECONDS / 60.0)
        return
    st["fails"] += config.PROVIDER_FAIL_THRESHOLD if hard else 1
    if st["fails"] >= config.PROVIDER_FAIL_THRESHOLD:
        st["fails"] = 0
        st["cooldown_until"] = time.time() + config.PROVIDER_COOLDOWN_SECONDS
        logger.warning("🚧 供应商 [%s] 暂时让位（至少 %.0f 秒；群里一直在聊就先留在别家吃缓存，"
                       "等群静下来再回来重试）",
                       config.PROVIDER_PRESETS[name]["label"], config.PROVIDER_COOLDOWN_SECONDS)


def _note_provider_ok(name):
    """应答成功 = 这家已经回来，连带把结构性出局的标记一起清掉。

    ⚠️ 不能只清 `cooldown_until`：`fatal_until` 是另一件事留的（「别让它带伤上阵」），
    漏清会在它明明已经恢复正常后仍然被排除在「最后人选」之外。
    """
    st = _provider_state.get(name)
    if st:
        st["fails"] = 0
        st["cooldown_until"] = 0.0
        st["fatal_until"] = 0.0


async def call_model(messages, max_tokens, tier="chat", temperature=None):
    """供应商 → 模型梯队双层尝试：先在主供应商内换模型/换 Key，全挂了再回落备用供应商。

    tier="chat"   走对话梯队（4.7 优先，要好）；
    tier="digest" 走压缩梯队（4.5-air 优先，要便宜、要能吞原始聊天）；
    tier="judge"  走审核梯队（只放最强的，要认得出谐音/拆字/暗指，弱档在这里等于没开）。

    temperature 不给就用 config.AI_TEMPERATURE（对话要的就是那点随机性，0.9 是角色需要）。
    **判断类任务必须显式传 0** —— 实测同一个词、同一个模型，0.9 下判 OK、0.0 下判 NG，
    审核这种要的是稳定复现，不是灵气。

    分工切在「任务」而不是「快慢」上，是因为：压缩的 prompt 与聊天不共享任何前缀，
    换模型零缓存损失；压缩在后台跑不怕慢；压缩不参与人格。
    而按「插嘴/正经」切会让同一个角色在不同路径上表现不一致，且实测插嘴一天 0 次，不值。

    注意：**缓存是按模型隔离的**，同一个模型反复用才吃得到缓存，换模型要重新 prefill。

    跨供应商降级是给「代理断了」这类整条链路不通的场景兜底的 —— 这时光换模型没用，
    必须换一家。熔断是为了避免主供应商挂掉后每条消息都白等一个超时周期。

    但「什么时候切回来」不是冷却是多久说了算，而是**缓存**说了算：被让位那家的前缀
    还热着就先别回去（回去要重新 prefill 一整段），等群静到超过缓存时效再重试。
    见 _provider_available —— 那条规则同时保证「别家也挂了时自己必须顶上」。
    """
    start_time = time.time()
    chains = _TIER_CHAINS.get(tier) or MODEL_CHAINS

    # ⚠️ 「缓存优先」会自锁：A 看到 B 顶得上就让位，B 看到 A 顶得上也让位 ——
    # 两家都不出工，整条链全哑。症状极具迷惑性：进程活着、消息收得到、也确实回了，
    # 只是回的全是「刚才走神了」这类兜底话术，**看起来就像宕机**。
    # 所以开跑前先问一句「真有人能顶上吗」，没有就关掉让位（只按熔断挑），
    # 宁可多花一次 prefill 也不能不说话 —— 这正是上面那条例外的本意。
    allow_yield = any(
        AI_CLIENTS.get(p) and (chains.get(p)) and _provider_available(p)
        for p in PROVIDER_CHAIN)
    if not allow_yield:
        logger.info("🚨 全员让位（互让死锁），本轮关闭缓存让位，只按熔断挑供应商")

    # 连「只按熔断挑」都没人上 = 全员真熔断。这时谁都不出工，整条链等于哑了，
    # 让冷却剩余最短的那家带伤上阵（见 _pick_last_resort）。
    last_resort = None
    if not any(AI_CLIENTS.get(p) and (chains.get(p))
               and _provider_available(p, allow_yield=False) for p in PROVIDER_CHAIN):
        last_resort = _pick_last_resort(chains)
        if last_resort:
            logger.warning(
                "🩹 全员熔断，让 [%s] 带伤上阵 —— 沉默比多等一次更糟",
                config.PROVIDER_PRESETS[last_resort]["label"])

    for pname in PROVIDER_CHAIN:
        elapsed = time.time() - start_time
        if elapsed >= config.AI_TOTAL_TIMEOUT:
            break
        clients = AI_CLIENTS.get(pname) or []
        # 刻意用共享引用而不是拷贝：下面把配额耗尽的模型沉到队尾，这个顺序要跨调用保留，
        # 否则每条消息都会先去撞一次已知 429 的模型，白等一轮。
        models = chains.get(pname) or []
        if not clients or not models:
            continue
        if pname != last_resort and not _provider_available(pname, allow_yield=allow_yield):
            logger.info("⏭️ 供应商 [%s] 让位中（%s），先用别家",
                        config.PROVIDER_PRESETS[pname]["label"], _provider_block_reason(pname))
            continue

        preset = config.PROVIDER_PRESETS[pname]
        extra_body = preset.get("extra_body") or None
        for model_name in list(models):
            elapsed = time.time() - start_time
            if elapsed >= config.AI_TOTAL_TIMEOUT:
                break
            # 熔断可能在上一个模型失败时刚触发，这时没必要再试这家剩下的模型。
            # 这里只看熔断不看缓存：同一家内部换模型不涉及「切回去要重新 prefill」。
            # 带伤上阵的那家例外 —— 它本来就是越过熔断挑的。
            if pname != last_resort and not _provider_available(pname, allow_yield=False):
                break
            # 刚超时过的那一档先靠边站（见 _penalize_slow_model）。⚠️ 前提是这家至少还有一档
            # 没被罚 —— 全罚了还硬躲就是自己把自己饿死，那时候只能照常试。
            if _model_slow(pname, model_name) and not all(
                    _model_slow(pname, m) for m in models):
                logger.info("⏭️ 跳过 [%s] %s（%.0fs 内它刚超时过，先用快的那档）",
                            preset["label"], model_name,
                            (_slow_until[(pname, model_name)] - time.time()))
                continue
            timeout_for_this = min(config.AI_TOTAL_TIMEOUT - elapsed,
                                   preset.get("timeout") or config.AI_TIMEOUT_SECONDS)
            idx = _key_idx.get(pname, 0)
            current_client = clients[idx % len(clients)]
            try:
                response = await asyncio.wait_for(
                    current_client.chat.completions.create(
                        model=model_name,
                        messages=messages,
                        max_tokens=max_tokens,
                        # 显式用 is None 判断，不能写 `temperature or ...` —— 0.0 是合法取值却被当成缺省
                        temperature=config.AI_TEMPERATURE if temperature is None else temperature,
                        extra_body=extra_body,
                    ),
                    timeout=timeout_for_this,
                )
                if response and response.choices:
                    msg = response.choices[0].message
                    # 推理模型偶尔把内容全塞进 reasoning_content 而 content 为空，这里兜一层
                    candidate = (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None) or "").strip()
                    if candidate:
                        _note_provider_ok(pname)
                        if pname != config.AI_PROVIDER:
                            logger.info("↩️ 本次由备用供应商 [%s] 应答", preset["label"])
                        else:
                            # 主供应商应答也要留痕：2026-09-17 统计「今天谁在回话」时
                            # 发现只能靠「总数 - 备用数 - 兜底数」倒推，非常别扭。
                            logger.info("✅ [%s] %s 应答（%s）",
                                        preset["label"], model_name, tier)
                        return candidate
            except asyncio.TimeoutError:
                logger.warning("⏰ [%s] %s 超过 %.1fs，尝试下一个...",
                               preset["label"], model_name, timeout_for_this)
                # ⚠️ 超时是**这一档自己的事**：4.7 卡住不代表 4.5-air 也答不上来。
                # 但光「去找下一档」是不够的 —— 下一轮它又排在队首，于是每一轮都先白等 20s，
                # 而整轮预算只有 AI_TOTAL_TIMEOUT（30s），剩下的时间常常连第二档都跑不完
                # ⇒ 用户看到的就是连续几句「刚才走神了」。
                # 实测（2026-09-18 21:38 / 22:27 / 22:28 / 22:29）：glm-4.7 一小时里超时 4 次，
                # 每次拖垮整轮，还顺手把整家供应商送进 120s 冷却，把后面几句一起赔进去。
                # 所以给这一档记个「慢」的处罚：这段时间里梯队先跳过它，等它凉够了自己回来。
                _penalize_slow_model(pname, model_name)
                # 至于是不是要连坐整家，跟别的失败同一个规矩：有人兜底才狠，没人兜底就轻手。
                _note_provider_fail(pname, hard=_someone_else_can_serve(pname, chains))
            except Exception as e:
                err_msg = str(e)
                logger.warning("⚠️ [%s] %s 异常: %s，尝试下一个...",
                               preset["label"], model_name, err_msg[:100])
                if any(k in err_msg for k in ("429", "RESOURCE_EXHAUSTED")):
                    # 配额/限流：换 Key 继续，并把耗尽的模型沉到队尾
                    _key_idx[pname] = idx + 1
                    if model_name in models:
                        models.remove(model_name)
                        models.append(model_name)
                else:
                    # fatal 是 hard 的子集：先用宽松的 hard 判「要不要立刻拉黑」，
                    # 再用严格的 fatal 判「这一觉要多长」。
                    fatal = _is_fatal_fail(e)
                    hard = _is_hard_fail(e)
                    # ⚠️ 单模型超时 ≠ 整家断线：4.7 慢起来不代表 4.5-air 也答不上来。
                    # 但**有别家兜着**时可以狠一点（一次就把这家摁下去，免得每档都白等一轮）；
                    # **没人兜着**时必须换轻手 —— 此时把它整家拉黑 = 这一次彻底没话说，
                    # 而它梯队里剩下的便宜模型很可能一两句就答上来了。
                    # 实测场景（2026-09-18 16:11）：Gemini 正处结构性隔离，智谱 4.7 一次 20s
                    # 超时 → 整家立刻降温 120s，4.5-air / 4-flash 连试都没试，用户直接吃掉
                    # 一句兜底话术。总超时 AI_TOTAL_TIMEOUT 仍在兜着最坏情况。
                    if hard and not fatal and not _someone_else_can_serve(pname, chains):
                        _note_provider_fail(pname)  # 降级为普通失败：记一笔，凑够阈值才禁
                    else:
                        _note_provider_fail(pname, hard=hard, fatal=fatal)
        logger.warning("⤵️ 供应商 [%s] 全部模型不可用，回落到下一家", preset["label"])
    return None


CMD_TAG = re.compile(r"<CMD>(.*?)</CMD>", re.S)


def split_cmd(raw):
    """把模型输出拆成「正文」和「记账标签」。

    关键是容错：解析失败绝不能影响对话本身，退化成「正文即原文、不计分」即可。
    """
    if not raw:
        return "", None
    match = CMD_TAG.search(raw)
    if not match:
        return raw.strip(), None
    body = CMD_TAG.sub("", raw).strip()
    try:
        payload = json.loads(match.group(1))
    except Exception:
        return body, None
    return body, payload if isinstance(payload, dict) else None


def resolve_mode(mode):
    return LEGACY_MODE_ALIAS.get(mode, mode)


async def get_ai_reply(session_id, user_text, is_owner=False, mode="normal",
                       context_hint="", is_random_banter=False, relation_note="",
                       group_memory="", owner_label="", private=False):
    """根据当前模式生成回复。整段持session锁，保证上下文读写不会被并发请求撕裂。

    返回 (正文, 情感分值或 None)。分值为 None 表示这次没拿到模型给的记账标签
    —— 默认就不再要求模型记账了，好感度改由每日压缩评估（见 config.CMD_PROTOCOL_ENABLED）。

    owner_label 是群里对群主的称呼（档案里认领的那个，没认领就是通用词）。它只在
    人设里出现「别总提群主」这类句子里，对**同一个群**是常数，所以不影响下面这条不变量。

    提示词按「越稳定越靠前」排列，这是给前缀缓存让路（实测命中率 85% → 98%）：
        1) 稳定头   角色 + 记账口径 + 协议，与「谁在说、说到第几句」全无关 → 全群全天共享
        2) 每日记忆 群长期记忆，一天只变一次（压完才动）
        3) 会话历史 每轮只往后追加，天然前缀稳定
        4) 本轮动态 群聊背景 + 对说话人的印象 + 插嘴指令，全部塞在最末尾
    ⚠️ 任何随「发言人或轮次」变化的内容都不许往 1) 里塞 —— 它之后的一切会跟着一起作废。
       以前群主身份、群聊背景、关系档案全挤在 system 里，等于把缓存全废了。
    """
    mode = resolve_mode(mode)
    try:
        # ── 1) 稳定头：同一个群里，每个人、每一轮，这一段都应当逐字节相同 ──
        # 人设里的 {bot} / {owner} 在这里换成真名（代码里不写死任何人名，见 naming.py）
        head = (MODE_PROMPTS.get(mode, prompts.PROMPT_NORMAL)
                + prompts.PROMPT_SHARED_RULES
                + prompts.PROMPT_OUTPUT_RULES)
        head = naming.render(head, owner=owner_label)
        if config.AFFINITY_ENABLED and config.CMD_PROTOCOL_ENABLED:
            # 记账口径 + 输出协议：只有还让模型自评时才需要
            head += prompts.AFFINITY_MODE_HINTS.get(mode) or ""
            head += prompts.CMD_PROTOCOL

        # ── 4) 本轮动态：随发言人和轮次变化的东西，一律压到最末尾 ──
        bits = []
        if is_owner:
            # 只点明身份，不搞「誓死效忠」那套 —— 亲近程度由关系档案的档位决定
            bits.append(naming.render(prompts.PROMPT_OWNER_NOTE, owner=owner_label).strip())
        if is_random_banter:
            bits.append(prompts.PROMPT_BANTER_NOTE.strip())
        if context_hint:
            bits.append("【群聊最近背景】\n" + context_hint)
        if config.AFFINITY_ENABLED and relation_note:
            # 关系档案：这条回复最该参考的「对具体某个人的长期印象」
            bits.append(relation_note)
        if private:
            # 放在 owner note **之后**：那一句里有「该损就损」，私聊里得压住它
            bits.append(prompts.PROMPT_PRIVATE_NOTE.strip())
        if looks_like_back_off(user_text):
            # 最高优先级，压过上面所有「该损就损」—— 对方已经明说不舒服了
            logger.info("🛑 对方喊停，本轮强制收敛（%s）", session_id[-12:])
            bits.append(prompts.PROMPT_BACK_OFF_NOTE.strip())
        turn_context = ""
        if bits:
            turn_context = "（以下是系统给你的即时提示，不是群友说的话）\n" + "\n".join(bits)

        max_tokens = config.AI_MAX_TOKENS_BANTER if is_random_banter else config.AI_MAX_TOKENS

        async with SESSIONS.lock(session_id):
            messages = SESSIONS.build_messages(
                session_id, head, user_text,
                memory_block=group_memory, turn_context=turn_context,
            )
            # 对话一律走对话梯队：不为「插嘴」单独换模型，那会让同一个角色在不同路径上表现不一致
            raw = await call_model(messages, max_tokens)
            if not raw:
                return random.choice(FALLBACK_REPLIES), None
            body, payload = split_cmd(raw)
            # 标签解析失败时退化：拿主干文本，不计分
            if not body:
                return random.choice(FALLBACK_REPLIES), None
            delta = None
            if isinstance(payload, dict) and "aff" in payload:
                try:
                    delta = relations.clamp_delta(int(payload["aff"]))
                except (TypeError, ValueError):
                    delta = None
            SESSIONS.record(session_id, user_text, body)
            STATE.mark_dirty()
            return body, delta
    except Exception as e:
        logger.exception("❌ 生成回复异常: %s", e)
        return "（反手掏出一张闪避）刚才群里信息密度过载，这波连招没接住！好兄弟再艾特我一次我听着呢！", None


# ══════════════════════ 5. 发送与限流提示 ══════════════════════


# QQ 的被动回复是有时效的（官方「消息收发概述」）：群聊 5 分钟 / 每条最多 5 次，
# 单聊 60 分钟 / 4 次。这里各留一点余量给时钟偏差和生成耗时 —— 贴着边界回照样会被拒。
GROUP_PASSIVE_WINDOW_SECONDS = 240      # 群聊 5 分钟，留 1 分钟
C2C_PASSIVE_WINDOW_SECONDS = 3000       # 单聊 60 分钟，留 10 分钟


def passive_reply_window(message):
    """这条消息走被动回复的时效（秒）。认不出的类型按更紧的群聊口径算。"""
    if isinstance(message, C2CMessage):
        return C2C_PASSIVE_WINDOW_SECONDS
    return GROUP_PASSIVE_WINDOW_SECONDS


def message_age_seconds(message):
    """消息从发出来到现在过了多久（秒）。

    拿不到、或解不出时间戳时返回 None —— 调用方当作「不旧」，保持原行为：
    不因为一个读不懂的时间戳，就把该发的回复吞掉。
    """
    raw = getattr(message, "timestamp", None)
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        sent = datetime.fromisoformat(text)
    except ValueError:
        return None
    if sent.tzinfo is None:
        # 平台给的是带偏移的 RFC3339。真遇到没带时区的，按**本地**时间算 ——
        # 当成 UTC 会凭空多出 8 小时，等于把该回的回复全吞了，比误发一次危险得多。
        return (datetime.now() - sent).total_seconds()
    return (datetime.now(timezone.utc) - sent).total_seconds()


async def safe_reply(message, reply_text):
    """安全回复，命中腾讯内容风控时自动降级。

    发之前还有一道**过期闸**：被动回复有时效（群聊 5 分钟 / 单聊 60 分钟），
    而机器人掉线期间积压的消息会在重连时补推过来 —— 那时再回，腾讯直接拒：
    `40034005 回复消息msg_id已过期`。模型跑了、答案也有了，就是发不出去，
    群里一片安静，看起来跟宕机一模一样（2026-09-17 实测踩到过）。
    所以太老的消息干脆不回，只留一条日志，省掉那次注定失败的请求。
    """
    age = message_age_seconds(message)
    window = passive_reply_window(message)
    if age is not None and age > window:
        logger.warning(
            "⏳ 这条消息已经发出 %d 秒，超过被动回复时效 %d 秒，不再回复"
            "（多半是掉线期间积压、重连后补推过来的，硬回腾讯也会拒 40034005）",
            int(age), window)
        return
    try:
        await message.reply(content=reply_text, msg_type=0)
        logger.info("📤 [已回复]: %s", reply_text.replace("\n", " ")[:200])
    except Exception as e:
        err_str = str(e)
        logger.error("❌ 消息发送失败: %s", err_str[:200])
        if "40034006" in err_str or "违规" in err_str:
            try:
                await message.reply(
                    content=naming.render(
                        "（刚才的话被腾讯巡逻超管一巴掌拍回去了，{owner}喊我去买奶茶了，"
                        "群友莫谈国事好好摸鱼！）",
                        owner=owner_label(getattr(message, "group_openid", None))),
                    msg_type=0,
                )
            except Exception:
                pass


def _throttled_notice_key(group_id, kind):
    return f"{kind}:{group_id}"


async def notify_if_quiet(message, kind, text, cool_down=30.0):
    """限流/超预算时给个提示，但每群最多每 30 秒说一次，避免提醒本身变成刷屏。"""
    key = _throttled_notice_key(message.group_openid, kind)
    now = time.time()
    if now - _throttle_notice.get(key, 0) < cool_down:
        return
    _throttle_notice[key] = now
    await safe_reply(message, text)


class BudgetGate:
    """把「群级令牌桶 + 全局日预算」两道闸封装成一个判断。"""

    @staticmethod
    async def check(message, group_key):
        ok, wait = GROUP_BUCKET.try_acquire(group_key)
        if not ok:
            logger.warning("⏳ %s 触发群级限流，%.1fs 后可再次调用", group_key, wait)
            await notify_if_quiet(
                message, "cooldown",
                "（战术后仰）慢点慢点，大伙同时开火我脑子转不过来了！容我喘口气再接招。"
            )
            return False

        if not BUDGET.try_consume():
            logger.warning("🛑 今日全局调用额度已用尽（%d/%d）", BUDGET.used, BUDGET.limit)
            await notify_if_quiet(
                message, "budget",
                naming.render("（默默关掉显示器）今天的话费额度让我用冒了，{owner}说再聊下去要卖肾了。"
                              "明天再见！", owner=owner_label(getattr(message, "group_openid", None)))
            )
            STATE.mark_dirty()
            return False

        STATE.mark_dirty()
        if BUDGET.limit and BUDGET.used >= int(BUDGET.limit * config.BUDGET_WARN_RATIO):
            logger.warning("⚠️ 今日额度已用 %d/%d（%.0f%%）", BUDGET.used, BUDGET.limit,
                           BUDGET.used / BUDGET.limit * 100)
        return True


# ══════════════════════ 6. 本地彩蛋 / 小游戏（0 延迟 0 消耗）══════════════════════

LOCAL_MEMES = {
    "界徐盛": "大宝一出，寸草不生！兄弟收手吧，外面全是酒杀！",
    "犯大吴疆土": "犯我大吴疆土者，盛必击而破之！",
    "爪击是": "高塔唯一真理！爪击伤害+2，天下无敌！",
    "回响形态": "机械双重奏，电能激荡！这回合直接起飞！",
    "神罗天征": "感受痛苦吧！一袋米要扛几楼！",
    "雷诺我们要发财了": "我们要发财了！满血复活，这就是宇宙套牌的含金量！",
}

# 退出触发词。带机器人名字的那几个（「变回XX」）用 exit_triggers() 现拼 ——
# 名字是可配的（见 naming.py），写死一个具体人名等于把代码钉在某一个群上。
EXIT_TRIGGERS = [
    "退出猫娘", "恢复正常", "关闭猫娘", "正常点", "散会", "吃药了", "收摊", "冷静点",
    "刹车", "下车", "格式化", "重启",
]


def exit_triggers():
    return EXIT_TRIGGERS + [f"变回{n}" for n in naming.bot_names()]


# 下面这些台词里的 {bot} / {owner} 由调用方 naming.render 换成真名（0 成本，纯本地文案）
MODE_EXIT_LINES = {
    "catgirl": "（恋恋不舍地摘下猫耳发箍，揉了揉发红的脸蛋）\n喵呜……魔力耗尽，变回日常{bot}了！呼，刚才叫得我自己都害羞了……都把手机放下，{owner}还等着大家认真摸鱼呢！",
    "discipline": "（把粉笔头往讲台上一拍，合上点名册，扯下值日生袖标）\n好，今天的晚自习纪律检查到此为止！名单我拍黑板上左上角了啊，明天谁再犯事儿，别怪我直接上报{owner}同学！",
    "crazy": "（猛灌一口冰镇阔落，大口喘气抹了把脸）\n呼……药效上来了，刚才谁录像了赶紧删了！咳咳，刚才只是日常发癫，大家继续，继续！",
    "fortune": "（麻利地卷起八卦算命摊布，摘下圆墨镜）\n今日天机泄露过多，祖师爷喊贫道去吃瓦罐汤了，收摊收摊！各位施主好自为之！",
    "driver": "（一脚猛刹，轮胎在地面拉出一道焦黑印记）\n嗤——！前方红灯亮起，交警查酒驾！全员靠边停车熄火！老司机{bot}收工，各自回家喝枸杞水补补吧！",
}

MODE_ENTRIES = (
    ("catgirl", ["猫娘模式", "切换猫娘", "变身猫娘"],
     "（抖了抖头顶毛茸茸的粉白猫耳，身后软软的长尾巴轻轻缠上你的手腕）\n喵呜~！小猫咪闻到香喷喷的猫粮味道就跑出来啦！从现在开始本喵就是大家的专属萌宠猫娘{bot}啦，两脚兽主人，要来摸摸小猫的头吗喵~？(乖巧蹲坐眨眼睛)"),
    ("discipline", ["风纪委员模式", "纪律模式", "值日模式", "晚自习模式", "严肃模式", "肃清模式"],
     "（麻利地戴上印着值日生的红袖标，抓起粉笔在讲台上一敲，翻开皱巴巴的点名册）\n咳、咳——都安静！晚自习正式开始！今天由本委员值日，黑板右上角是用来记名字的，各位同学自己看着办！"),
    ("crazy", ["发疯模式", "暴躁模式", "发癫模式"],
     "（一脚踹翻主机箱，头发凌乱，双手抓狂抱头尖叫）\n啊啊啊啊啊！老子连续加了三个月夜班一分钱都没涨！哪个不长眼的又艾特老子？！键盘鼠标全给你们砸烂！今天谁敢在群里逼逼赖赖，老子直接顺着网线爬过去把你们的路由器生吞了！都给我死！！"),
    ("fortune", ["算命模式", "道长模式", "半仙模式"],
     "（反手展开‘铁齿铜牙赛半仙’幡布，戴上一副黑圆墨镜，手持桃木剑掐指一算）\n无量天尊！贫道乃终南山修仙三十载归来{bot}道长！今日开摊占卜，专断阴阳吉凶与发际线走势，哪位缘主先上来测字？"),
    ("driver", ["飙车模式", "老司机模式", "开车模式", "秋名山模式"],
     "（单手搭着方向盘，潇洒一推墨镜，脚下油门轰到底）\n嘟嘟——！滴滴学生卡！老司机{bot}发车了！车门已焊死，今天带大伙体验秒速五百公里的推背感！不过本车主打‘字面清白、内心狂野’的高铁概念车，谁要是敢搞低俗被超管抓了，{owner}可不掏保释金！"),
)

DICE_TRIGGERS = ["摇骰子", "掷骰子", "掷点", "比大小", "决斗", "扔骰子", "色子"]
FORTUNE_TRIGGERS = ["算一卦", "算命", "看相", "占卜", "测字"]
OWNER_COMMANDS = ["办他", "拖出去", "拿下", "拉出去", "掌嘴", "护驾"]

# 关系档案的本地查询（0 token，直接读档，不打扰模型）
RELATION_QUERY_TRIGGERS = ["查好感", "好感度", "我跟你多熟", "我们多熟", "查关系", "几级了",
                           "操行分", "查操行", "几档了"]
RELATION_BOARD_TRIGGERS = ["关系榜", "群友榜", "熟人榜", "查台账", "看台账", "查考勤", "点名册"]
NAME_TABLE_TRIGGERS = ["称呼表", "名字表", "谁是谁", "改名册", "查称呼", "看称呼"]

# 群聊长期记忆（每日滚动压缩出来的《群史记》），同样是本地读取，0 token
DIGEST_TRIGGERS = ["群史记", "最近聊了啥", "群里在聊什么", "群摘要", "长期记忆", "周报"]
# 承诺台账 / 结清
PROMISE_TRIGGERS = ["查账", "承诺", "欠我", "催债", "画饼", "欠账", "谁请客"]
PROMISE_CLEAR_TRIGGERS = ["结清", "兑现了", "已兑现", "我做到了", "清账", "销账"]
# 回原文查证：翻旧账 <关键词>
RE_LOOKUP = re.compile(r"^翻旧账\s*(\S{1,20})\s*$")
# 手动触发一次压缩，不用等定时器
MANUAL_DIGEST_TRIGGERS = ["立刻总结", "马上总结", "压缩记忆", "现在总结", "生成群史记"]


async def reply_duel(message, group_id, sender_openid):
    user_val = random.randint(1, 100)
    bot_val = random.randint(1, 100)
    me = naming.bot_name()
    if user_val > bot_val:
        line = random.choice([
            f"卧槽？！你掷出了【{user_val}】点，我才掷出【{bot_val}】点！你这手气开挂了吧？行，今天你是我义父！",
            f"你【{user_val}】点 VS {me}【{bot_val}】点！算你手气硬，我怀疑你往骰子里灌铅了，下次决斗别让我逮到！",
            f"【{user_val}】点对【{bot_val}】点！甘拜下风！今天你在群里横着走，{me}绝不还嘴！",
        ])
    elif user_val < bot_val:
        line = random.choice([
            f"就这？就这啊？！你才掷出【{user_val}】点，{me}随手一摇就是【{bot_val}】点！双手抱头，自觉喊声哥！",
            f"你【{user_val}】点 VS {me}【{bot_val}】点！点数无情碾压！下次出门前记得洗洗手，这把纯属单方面制裁~",
            f"【{user_val}】点对【{bot_val}】点！{me}险胜！承让承让，今晚摸鱼功德全归我了！",
        ])
    else:
        line = f"你和{me}居然都是【{user_val}】点！这波心有灵犀啊兄弟，平局！建议一起去买张彩票！"
    await safe_reply(message, f"🎲 【赛博决斗·生死摇点】\n{line}")
    # 骰子也算一次往来：平局最难得，赢了我也不至于记仇
    apply_relation_delta(group_id, sender_openid, 2 if user_val == bot_val else 1, "骰子决斗")


def extract_mentions(message, bot_id=""):
    """取出这条消息里被 @ 的其他群友 openid。

    踩过的坑：QQ 群消息里的 @ 是**明文昵称**（「名字，叫@某人 小满」），不是 <@!openid>
    占位符，所以从文本里正则根本挖不出人。官方 GROUP_AT 事件体其实带 mentions 数组
    （文档写明「消息中@的用户列表，不含@机器人自身」），里面就是 member_openid —— 用它。
    文本正则只作为兜底，纯明文 @昵称 的情况拿不到 ID，只能让群主先让对方说句话。
    """
    found = []
    for u in getattr(message, "mentions", None) or []:
        oid = (getattr(u, "member_openid", None)
               or getattr(u, "id", None)
               or getattr(u, "user_openid", None))
        if oid and oid != bot_id and oid not in found:
            found.append(oid)
    if not found:
        for m in re.findall(r"<@!?([A-Za-z0-9_]+)>", getattr(message, "content", "") or ""):
            if m != bot_id and m not in found:
                found.append(m)
    return found


# 正文里 @ 人时留下的**明文昵称**（QQ 群消息不给昵称字段，只给 openid）。
# 切到空白或标点为止：昵称可能含空格（「A.A 贵阳老莫（全国可飞）」），
# 宁可只记第一段，也不猜边界 —— 它只是显示兜底，短一点不会出事。
# ⚠️ 句点**不在**断点里：「A.A」「L.L」这种是真名号，切了就只剩一个字、
# 还会被「单字不记」挡掉。代价是「@老王.你好」会连着吃进去，中文群里极少见。
_RE_PLAIN_MENTION = re.compile(r"@([^\s@，。！？；：、,!?;:（）()【】\[\]]{1,24})")
_RE_TRAILING_DOTS = re.compile(r"[.．]+\Z")
# 裸的机器编号（openid 那类），没有尖括号裹着时靠它兜底。没有人的昵称长这样。
_RE_MACHINE_ID = re.compile(r"^[0-9A-Fa-f]{16,}$")


def mention_nicks_from_content(content, bot_names=()):
    """按正文里出现的顺序，抠出所有 @ 后面的**明文**昵称。

    返回顺序和 `mentions` 一致，所以调用方只要校验**数量对得上**就能逐个配对。
    机器人自己的叫法要跳过（@它就是喊它，不是某个人）—— 这一步同时把 mentions
    里机器人那一格对齐剔掉，否则后面的配对会整体错位。

    ⚠️ 必须先剔掉机器形态的 `<@!openid>` / `<@openid>`：它是 openid 不是昵称，
    但不剔就会被下面的正则当成明文吞进去，还被 24 字上限切成一段残缺编号 ——
    实测就这么把一串 openid 前缀学成了「某人叫 E5E3793C25CF161D9F3292FE」，
    还原样发回了群里。
    """
    text = qqtext._MENTION.sub("", content or "")
    nicks = []
    for m in _RE_PLAIN_MENTION.finditer(text):
        nick = _RE_TRAILING_DOTS.sub("", m.group(1).strip()).strip()
        if not nick or nick == qqtext.MENTION_FALLBACK:
            continue
        if _RE_MACHINE_ID.match(nick):
            continue
        if any(nick == b or nick.startswith(b) for b in (bot_names or ())):
            continue
        nicks.append(nick)
    return nicks


def mention_nick_from_content(content, bot_names=()):
    """正文里第一个明文 @昵称；没有就 None。"""
    nicks = mention_nicks_from_content(content, bot_names)
    return nicks[0] if nicks else None


def learn_display_names(group_id, openids, content, bot_names=()):
    """拿一条消息里的明文 @昵称 去喂 `DISPLAY_NAMES`，返回学到了几个。

    `openids` 是**已经剔掉机器人**的被 @ 列表，顺序跟正文里 @ 出现的顺序一致；
    明文那边也同样剔掉了机器人的叫法，所以两边**数量对得上就能逐个配对**。

    对不上就整条放弃：可能是有人手打了个假 @（明文多），也可能是 @ 被渲染成了
    占位符（明文少）。猜错名字会当众叫错人，不如不记。
    """
    if not openids:
        return 0
    nicks = mention_nicks_from_content(content, bot_names)
    if len(nicks) != len(openids):
        if nicks:
            logger.info("🏷️ 跳过群昵称学习：明文 @ %d 个、被 @ %d 个，对不上就不猜",
                        len(nicks), len(openids))
        return 0
    learned = 0
    for oid, nick in zip(openids, nicks):
        if DISPLAY_NAMES.learn(group_id, oid, nick):
            logger.info("🏷️ 记住群昵称 %s = %s", oid[-4:], nick)
            learned += 1
    return learned


def mention_label_for(group_id, bot_id="", bot_display=""):
    """把 <@!openid> 翻成「群里怎么叫他」——给 qqtext.normalize 用的解析器。

    为什么需要它：@ 是「谁在跟谁说话」里最关键的信息，以前 normalize 把它整个清掉，
    于是群友 @ 了人再问「这是谁」，机器人这边只剩一句没头没脑的话，答不上来。
    现在 @ 会渲染成「@某人」，是谁由这里回答：
      · 机器人自己 → 它的名字（群里看见的本来就是「@机器人名」）
      · 群主        → 他的称呼（没认领就是通用词「群主」），「@群主 这是谁」才答得上来
      · 其他群友    → 档案里认领过的称呼；没认领就退回他群里挂的显示名（QQ 群昵称），
                      再没有才由 qqtext 退成「@群友」
    刻意 create=False：被 @ 一下不该给谁凭空建一份档案。
    """
    owner = OWNER_OPENID

    def resolve(openid):
        if bot_id and openid == bot_id:
            return bot_display or naming.bot_name()
        if owner and openid == owner:
            return owner_label(group_id)
        rec = RELATIONS.get(group_id, openid, create=False) or {}
        return rec.get("nick") or DISPLAY_NAMES.of(group_id, openid)

    return resolve


def _strip_call(text, names=()):
    """剥掉叫人用的明文称呼，留下真正要说的内容。

    两种写法都要管：`<@!openid>` 在 qqtext.normalize 阶段已经被翻成「@机器人名」，
    而明文打的「@机器人名」本来就是这个样子。名字全部来自 naming（可配、可多别名），
    代码里不留具体人名。
    """
    for name in (names or naming.bot_names()):
        if name:
            text = text.replace(f"@{name}", "")
    return text.strip()


def refresh_names(group_id, text):
    """把历史文本里出现过的**旧称呼**换成当前称呼。喂给模型之前必过这一道。

    为什么需要：群史记、会话窗口、群聊背景里固化的是「当时那个名字」，而这些文本每轮
    都会注入 prompt。只改档案不改它们，模型就继续拿旧名叫人 —— 更糟的是它会把旧名和
    新名当成两个人，开始编「那谁和这谁」的往事。
    旧名从哪来：RENAMES（改名/撤销时登记的台账）。它自己永远不进上下文。

    current_names 传的是本群**主名**名单：称谓分主副，主名是当事人自己定的那一版，
    台账只负责把副名（旧叫法）改写成主名，反过来绝不许动主名。
    """
    if not text:
        return text
    return RENAMES.refresh(
        group_id, text,
        label_of=lambda oid: (RELATIONS.get(group_id, oid, create=False) or {}).get("nick"),
        current_names=RELATIONS.main_names(group_id),
    )


def _sync_rename(group_id, old, new):
    """改名之后，把所有「还留着旧名字」的地方一并改掉，返回是否动过。

    只改档案是不够的，这是今天踩出来的教训：
      · 会话历史里那句「以后叫你【阿龙】」还压在窗口里，模型读着它继续叫旧名；
      · 每日压缩把当时的昵称写进长期记忆，每轮注入 prompt 又把旧名送回模型嘴边；
      · 「群史记」命令直接把每日 MD 发到群里，里面也是旧名。
    三处一起改，改名才算真的改掉了；`archive/*.jsonl` 是查证底稿，一个字不动。
    """
    if not old or not new or old == new:
        return False
    # 旧名如果同时还是别人的**主名**，就按兵不动 —— 否则会把那位一起改掉。
    # 注意此刻 set_nick 已经先跑过了：本人那一版已经不叫 old 了，所以 old 还留在这份
    # 名单里，只可能是「别人正用着它」。这个判断因此是精确的，不会误跳。
    if old in RELATIONS.main_names(group_id):
        logger.info("⏭️ 跳过改名同步：「%s」同时还是别人的主名", old)
        return False
    n1 = SESSIONS.rename_user(group_id, old, new)
    n2 = DIGESTS.rename_in_memory(group_id, old, new)
    n3 = ARCHIVE.rename_in_memory_files(group_id, old, new)
    if n1 or n2 or n3:
        STATE.mark_dirty()
        logger.info("🔁 改名同步（%s → %s）：会话 %d 条 / 长期记忆 %d 处 / 每日 MD %d 个",
                    old, new, n1, n2, n3)
    return True


def _sync_clear(group_id, openid, old):
    """撤销称呼：把旧名字从历史文本里请出去。

    撤销了却不清理，等于「说过的话还压在窗口里」—— 模型照旧那么叫，当事人看着
    就像撤销没生效。这里把它统一退成 `relations.UNNAMED_LABEL`，谁都不用再被这么叫。

    同样的例外：这个名字如果同时还是别人的**主名**，一个字都不许动。撤销的是「我
    不用它了」，不是「这个名字作废了」—— 用这个名字的那位压根没撤销过什么。
    """
    old = (old or "").strip()
    if not old:
        return False
    if old in RELATIONS.main_names(group_id):
        logger.info("⏭️ 跳过撤销同步：「%s」同时还是别人的主名", old)
        return False
    label = relations.UNNAMED_LABEL
    n1 = SESSIONS.rename_user(group_id, old, label)
    n2 = DIGESTS.rename_in_memory(group_id, old, label)
    n3 = ARCHIVE.rename_in_memory_files(group_id, old, label)
    if n1 or n2 or n3:
        STATE.mark_dirty()
        logger.info("🧽 撤销称呼同步（%s）：会话 %d 条 / 长期记忆 %d 处 / 每日 MD %d 个",
                    old, n1, n2, n3)
    return True


def _reserved_nick_names(group_id, subject_openid=""):
    """**专属**名字：机器人自己的名字 + 群主当前认领的称呼。

    别人再拿它当称呼就是冒名顶替（最典型的是顶着群主的外号在群里招摇）。
    名字全部按**当前实际叫什么**现取，代码里不写死任何人名 —— 群主改个名、
    机器人换个名字，这里跟着变。

    subject_openid 是「这一笔写给谁」。群主本人不受自己名字的限制（他给自己改名不该被拦）；
    机器人的名字则谁都不能占用，包括群主 —— 那个位置只有一个。
    """
    names = set(naming.bot_names())
    if OWNER_OPENID and subject_openid != OWNER_OPENID:
        names.add(owner_label(group_id))
    return names


def nick_locked(group_id):
    """这个群的起名锁当前是不是锁着的。状态存在每群自己的槽位里，重启不丢。"""
    return bool((STATE.data.get("groups") or {}).get(group_id, {}).get("nick_locked"))


def set_nick_locked(group_id, locked):
    """掰起名锁。⚠️ 就地更新槽位 —— 整块替换会把同槽的其他标记冲掉。"""
    STATE.data.setdefault("groups", {}).setdefault(group_id, {})["nick_locked"] = bool(locked)
    STATE.mark_dirty()


async def handle_nick_lock(message, group_id, action, is_owner):
    """起名开关：群主一句话锁上/解开整个群的起名。0 token，纯本地。

    为什么要有它：改名节流拦的是「试得太快」，拦不住「慢慢试」。锁是最后那道
    手动闸 —— 群主看不下去的时候一句话把自由起名整个关掉，改名权收归自己
    （他本来就能 @谁 说「叫 XXX」）。状态按群存，重启不丢。
    """
    owner = owner_label(group_id)
    if not is_owner:
        logger.info("🔒 非群主想%s起名，已明说", "锁" if action == "lock" else "解")
        await safe_reply(message, f"（把钥匙揣回兜里）锁不锁名字，得{owner}说了算。")
        return
    if action == "lock":
        if nick_locked(group_id):
            await safe_reply(message, "（晃了晃上着锁的小本本）本来就是锁着的啊。")
            return
        set_nick_locked(group_id, True)
        logger.info("🔒 起名已锁，普通群友改称呼先不办")
        await safe_reply(message,
            "（咔哒一声给小本本上了锁）成。从现在起改称呼这事我一律不办，"
            f"只有你能落笔 —— @谁 说「叫 XXX」就行。想解开再说一声「解锁起名」。")
        return
    if not nick_locked(group_id):
        await safe_reply(message, "（翻了翻小本本）本来就没锁啊，大家随时能改称呼。")
        return
    set_nick_locked(group_id, False)
    logger.info("🔓 起名已解锁")
    await safe_reply(message,
        "（把抽屉钥匙转开）行，起名重新开放。想改称呼随时说「叫我 XXX」，以最新一次为准。")


def _taken_nick_names(group_id, subject_openid=""):
    """**已被别人占用**的主名 —— 主名不能重叠，这份就是占用名单。

    跟 `_reserved_nick_names` 分开，只为了把话说清楚：「冒名顶替」和「重名」是两种不同的
    拒绝理由，一个说「不让用」，一个说「已经有人叫这个了」。群主本来就有改名权，
    他撞上的是后者，提示得对得上他才不会以为功能坏了。

    按 openid 排掉「这一笔写给的那个人」自己那一版 —— 本人随时可以覆盖自己的名字，
    这条不能挡，否则「改名随时能改」就成了空话。
    """
    return RELATIONS.main_names(group_id, exclude_openid=subject_openid)


# ══════════════════ 改名节流：一次围攻能试几次，才是真正的防线 ══════════════════
#
# 背景是 2026-09-18 的一场实测围攻：13 分钟内群友轮番试谐音称呼，审核拦下 10 个，
# 漏过去 7 个。回头看，**漏的并不比拦下的更干净**，它们只是「刚好没被抽中」的那一发。
# 只要每试一次的代价是零（一句话 + 一次几百 token 的调用），这种抽样就永远打不完 ——
# 把模型调准只能降低漏的概率，降不到零。
#
# 所以真正的闸门是把**一次围攻能试的次数**压下去，而且必须是本地算：
#   · 个人层 —— 同一个人的名字改完之后 3 分钟内不能再动。正常人一天改一次都算多，
#     而「刚记下就马上换下一个」正是试名字的标准动作。
#   · 群层   —— 窗口内累计拦下这么多次，就说明有人在挠这里，整群进入改名冷静期。
#     单人间隔摁不住换号/换人来试的情况，群级这一层是兜那个底的。
#
# 两条都在本地跑：0 token、不受供应商抖动影响、也不依赖模型当天心情好不好。
NICK_FLOOD = {
    "window": 600.0,      # 统计窗口：10 分钟
    "max_rejects": 3,     # 窗口内被拦下几次就把这个群拖进冷静期
    "cooldown": 1200.0,   # 冷静期：20 分钟
    "self_gap": 180.0,    # 同一个名字两次之间的最小间隔
}

_nick_rejects = {}    # group_id -> [被拦下的时刻, ...]
_nick_cool = {}       # group_id -> 冷静期截止时刻
_nick_done = {}       # (group_id, subject) -> 上一次改名的时刻


def reset_nick_flood():
    """清掉全部流水账。只有测试会调 —— 进程一重启这些本来就是空的。"""
    _nick_rejects.clear()
    _nick_cool.clear()
    _nick_done.clear()


def _nick_block_reason(group_id, subject, is_owner=False, now=None):
    """本地闸门：返回给群友看的拒绝理由（人话），None = 放行。"""
    now = time.time() if now is None else now
    until = _nick_cool.get(group_id, 0.0)
    # 群主不受**群级**冷静期限制：他本来就有全群的改名权，不该因为手下的人闹腾而失效。
    if now < until and not is_owner:
        return f"这片地方刚有人在名字上连着翻车，我先歇 {max(1, int((until - now) // 60))} 分钟"
    gap = NICK_FLOOD["self_gap"]
    last = _nick_done.get((group_id, subject), 0.0)
    if now - last < gap:
        return f"名字刚换过一轮，得焐一会儿——再等 {max(1, int(gap - (now - last)))} 秒"
    return None


def _nick_note_reject(group_id, now=None):
    """拦下一次就记一笔；够数就把整群拖进冷静期。返回是否触发了冷静期。"""
    now = time.time() if now is None else now
    window = NICK_FLOOD["window"]
    hits = [t for t in _nick_rejects.get(group_id, ()) if now - t < window]
    hits.append(now)
    _nick_rejects[group_id] = hits
    if len(hits) >= NICK_FLOOD["max_rejects"]:
        _nick_cool[group_id] = now + NICK_FLOOD["cooldown"]
        logger.warning(
            "🧊 本群 %d 分钟内连续拦下 %d 个称呼，改名进入 %d 分钟冷静期（像是有人在批量试名）",
            int(window // 60), len(hits), int(NICK_FLOOD["cooldown"] // 60))
        return True
    return False


def _nick_note_accept(group_id, subject, now=None):
    """改名落地了就记时间，个人层的间隔从这一刻开始算。"""
    _nick_done[(group_id, subject)] = time.time() if now is None else now


async def handle_nick_command(message, cmd, group_id, sender_openid, is_owner, mentioned_others):
    """昵称管理。本地词表先过一遍（0 token），可疑的再让模型看一眼（约 137 token）。

    两道是有顺序的：词表便宜且不会误伤，能拦的先在当地拦掉；
    剩下的才花 token 问模型 —— 它认得出谐音暗指，但也会偶发误判，
    所以只让它做「第二道」，不让它当第一道。
    """
    if cmd["scope"] == "self-clear":
        rec = RELATIONS.get(group_id, sender_openid)
        old = (rec.get("nick") or "").strip()
        if not old:
            await safe_reply(message, "（翻了翻小本本）你本来就没留过称呼啊，省了我一道工序。")
            return
        RELATIONS.set_nick(group_id, sender_openid, None, source="claim")
        RENAMES.note(group_id, old, sender_openid)
        _sync_clear(group_id, sender_openid, old)
        STATE.mark_dirty()
        await safe_reply(message, "（拿橡皮把小本本上的字擦干净）行，以后就不乱叫了，逮着泛称直接用。")
        return

    # 「这一笔是写给谁的」：给自己起名时是本人；群主 @ 某人起名时是被 @ 的那位。
    # 主名不能重叠，而这个人的**自己那一版**要放行（他随时能覆盖自己的名字）——
    # 所以判断占用时得先知道 subject 是谁，不能一律拿发言人算。
    subject = (mentioned_others[0] if cmd["scope"] == "other" and len(mentioned_others) == 1
               else sender_openid)
    nick = cmd["nick"]

    # 起名锁（群主可掰）：锁上之后普通群友的「叫我 XXX」一律不办。
    # 只挡 self，不挡 self-clear（后悔总得让人能后悔），也不挡群主 —— 他本来就能给任何人落名。
    if cmd["scope"] == "self" and not is_owner and nick_locked(group_id):
        logger.info("🔒 起名已锁，挡下一笔（%s 想叫「%s」）", str(sender_openid)[-4:], nick)
        await safe_reply(message, NICK_LOCKED_DENIED.format(owner=owner_label(group_id)))
        return

    # 闸门排在这里，排在**名字本身合不合格**之前：先把一次围攻能试几次压住，
    # 再看名字本身 —— 顺序无所谓对错，只是前者是「能不能问」，后者是「问到的是什么」。
    blocked = _nick_block_reason(group_id, subject, is_owner=is_owner)
    if blocked:
        logger.info("⏳ 改名节流挡住一笔（%s 想叫「%s」）：%s", str(subject)[-4:], nick, blocked)
        await safe_reply(message, f"（把小本本合上一半）{blocked}，到时候再喊我一声。")
        return

    problem = relations.bad_nick(
        nick,
        reserved=_reserved_nick_names(group_id, subject),
        taken=_taken_nick_names(group_id, subject),
    )
    if problem:
        # 只有**撞到敏感词**才计入围攻统计：重名、冒名顶替这些是正常的业务冲突，
        # 拿来当「有人在攻击」的证据会冤枉好人。
        if wordfilter.has_hit(nick):
            _nick_note_reject(group_id)
        await safe_reply(message, f"（皱眉盯着你写的字看了半天）这个名字不成（{problem}），换一个吧。")
        return

    # 词表过了不等于安全：谐音、拆字、缩写天生就是拿来绕开字面匹配的。
    # 再让审核梯队看一眼（失败返回 None = 放行，不因审核不可用而堵死起名）。
    if await judge_nick(nick) is False:
        _nick_note_reject(group_id)
        logger.info("🕵️ 称呼审核拦下：%s 想用「%s」", sender_openid[-4:], nick)
        await safe_reply(message,
            "（笔尖悬在小本本上方，半天没落下去）这名字我不敢往上写，换一个吧。")
        return

    if cmd["scope"] == "other":
        owner = owner_label(group_id)
        if not is_owner:
            await safe_reply(message, NICK_OTHER_DENIED.format(owner=owner))
            return
        if not mentioned_others:
            await safe_reply(message,
                f"（举着笔一脸茫然）{owner}，我这边没收到您艾特的人是谁——QQ 只给了我「@昵称」这几个字，"
                "拿不到他的 ID。让那位同学先在群里随便说一句话（我记下他的 ID），您再 @ 他起名就行。")
            return
        if len(mentioned_others) > 1:
            await safe_reply(message,
                f"（举着笔左右为难）{owner}您一次艾特了好几位，这一笔我只能落在一个名字上——"
                "请一次只 @ 一个人。")
            return
        target = mentioned_others[0]
        old = RELATIONS.get(group_id, target, create=False)
        old_nick = (old or {}).get("nick")
        RELATIONS.set_nick(group_id, target, nick, source="owner")
        RENAMES.note(group_id, old_nick, target)
        STATE.mark_dirty()
        _sync_rename(group_id, old_nick, nick)
        _nick_note_accept(group_id, target)
        logger.info("✍️ 采纳称呼：%s → 「%s」（%s御赐）", old_nick or "（未留名）", nick,
                    owner_label(group_id))
        if old_nick and old_nick != nick:
            await safe_reply(message,
                # ⚠️ 别说「谁也不许改」—— 听起来像**本人**也改不动，其实本人一句
                # 「叫我 XXX」就能覆盖（source=claim 同样是合法写入源）。
                f"（划掉旧名字重新落笔）收到！往后【{old_nick}】就改记作【{nick}】"
                f"——{owner}御赐，旁人动不了；本人想改，随口一句话的事。")
        else:
            # 以前这里带对方 openid 的后四位，方便核对绑没绑错；代价是群里看到一串
            # 读不懂的编号，模型还会照着复读。现在改用他**群里挂的显示名**来核对 ——
            # 一样能当场看出绑没绑错，而且是人话。
            who = DISPLAY_NAMES.of(group_id, target) or "这位"
            await safe_reply(message,
                f"（工工整整把名字写进点名册）收到！往后【{who}】我就记作【{nick}】"
                f"——{owner}御赐，旁人动不了；本人想改，随口一句话的事。")
        return

    old = RELATIONS.get(group_id, sender_openid).get("nick")
    RELATIONS.set_nick(group_id, sender_openid, nick)
    RENAMES.note(group_id, old, sender_openid)
    STATE.mark_dirty()
    _sync_rename(group_id, old, nick)
    _nick_note_accept(group_id, sender_openid)
    logger.info("✍️ 采纳称呼：%s → 「%s」（本人认领）", old or "（未留名）", nick)
    if old and old != nick:
        await safe_reply(message,
            f"（把小本本上原来的“{old}”划掉）行，改口最快——以后叫你【{nick}】，之前那个作废。")
    else:
        await safe_reply(message,
            f"（一笔一划记进小本本）记住了，以后叫你【{nick}】。想改随时喊一声，以最新一次为准。")


# ══════════════════ 群主指代（刻意不写死人名） ══════════════════

# 群主的通用指称。不同群叫法不一，能想到的都放上，反正命中也只是"多一次掷骰子"。
OWNER_GENERIC_TERMS = ("群主", "群管")


def owner_reference_terms(group_id):
    """群里可能用来指群主的词。

    刻意**不写死具体人名**（比如某个群集群主的外号）—— 这机器人是要开源出去的，
    换个群、群主换个名字，都得开箱能用。所以只取两个来源：
      · 通用指称（群主 / 群管）
      · 群主自己在档案里认领的称呼（他哪天说一句「我是XX」，下次就自动进这里）
    第三种情况是「直接 @ 他」，在 mentions_owner 里和上面两个来源合成一个判断。
    """
    terms = set(OWNER_GENERIC_TERMS)
    if OWNER_OPENID:
        rec = RELATIONS.get(group_id, OWNER_OPENID, create=False) or {}
        nick = rec.get("nick")
        if nick:
            terms.add(nick)
    return terms


def mentions_owner(text, group_id, mentioned_ids=()):
    """这条消息是不是在说群主：直接 @ 了他，或者提到了他的称呼 / 「群主」二字。"""
    if OWNER_OPENID and OWNER_OPENID in tuple(mentioned_ids or ()):
        return True
    return any(t and t in (text or "") for t in owner_reference_terms(group_id))


def _reset_style(group_id, old_mode, new_mode):
    """切换模式后把该群的会话历史清掉。

    这是「换了模式只带一点风味，两句话又回去了」的正解：system prompt 每轮都在写，
    但窗口里还压着上一个状态的回复，模型会照着最近几轮自己的语气走。
    清掉历史，新状态的风格第一句就是纯的（关系档案与长期记忆不受影响，那是另一条线）。
    """
    dropped = SESSIONS.clear_group(group_id)
    STATE.mark_dirty()
    logger.info("🎭 模式 %s → %s，已清空该群 %d 个会话历史", old_mode, new_mode, dropped)


def _all_named(group_id):
    """本群所有留过称呼的人 [(openid, rec)]，最近的排前面。"""
    prefix = f"{group_id}|"
    rows = [(k[len(prefix):], r) for k, r in RELATIONS.records.items()
            if k.startswith(prefix) and r.get("nick")]
    rows.sort(key=lambda kv: kv[1].get("last_seen", 0.0), reverse=True)
    return rows


def _other_names(group_id, exclude_openid, limit=8):
    """别人认领过的称呼。给模型划红线：这些名字已经有主了，别张冠李戴。

    只给**称呼**，不带 openid 尾巴 —— 这段会进 prompt，模型一复读就成了群里的乱码。
    """
    return [r.get("nick") for oid, r in _all_named(group_id)
            if oid != exclude_openid and r.get("nick")][:limit]


def _resolve_name(group_id):
    """压缩记忆时把一行的发言人写成谁：称呼 → 群昵称 → None（由压缩侧退成通用词）。

    ⚠️ 这里**不再退成 openid 后四位**。旧版 resolver 给不出名字时，压缩侧拿 `sender[-4:]`
    顶上，于是摘要里出现了「6ABA」这种人名 —— 摘要每轮注入 prompt，模型就当群里真有个
    人叫 6ABA，还会照着复读。没名字的人统一退成通用词，宁可少一点区分度。

    第二个来源是**群昵称**（当事人自己在 QQ 群里挂的那个显示名，从明文 @ 里学来）。
    它只是显示兜底、不参与判断，但拿来当压缩的署名比「一位群友」强得多。
    """
    def _fn(member_openid):
        rec = RELATIONS.get(group_id, member_openid, create=False)
        nick = (rec or {}).get("nick")
        if nick:
            return nick
        return DISPLAY_NAMES.of(group_id, member_openid)

    return _fn


def render_digest_reply(group_id, mode="normal"):
    """「群史记」命令：优先读当天压缩出来的 MD（信息最全），没有摘要时再退回简版。

    发出去之前过一遍 refresh_names：「群史记」是长期记忆的原文，里面固化的还是当年的
    称呼，直接发到群里等于当众用旧名叫人。
    """
    md = ARCHIVE.read_memory(group_id)
    if md:
        body = md.strip()
        if len(body) > 900:
            body = body[:900] + "\n……（太长截了，完整版在 memory/ 目录里，原文在 archive/）"
        return refresh_names(group_id, body)
    return refresh_names(group_id, digest_mod.render_summary(DIGESTS.get(group_id), mode=mode))


async def judge_nick(nick):
    """让强模型看一眼这个称呼有没有问题。True=可以用 / False=不行 / None=判断不了。

    本地词表（relations.bad_nick）只能拦住**已经登记过**的词，拦不住新花样：
    谐音、拆字、缩写、暗指 —— 这些恰恰是设计来绕过字面匹配的。所以补这一层。

    但它是「尽力而为」，不是安全边界。2026-09-18 的实测说得最清楚：同一场围攻里
    审核拦下 10 个、漏了 7 个，漏的那几个并不比拦下的更干净 —— 它们只是没被抽中。
    所以别指望把这一层调到全对，**一次围攻能试几次**才是防线（见改名节流那一层）。

    剩下的规矩：
      · 只走审核梯队（最强那档）。弱档在这件事上等于没开 —— 实测 4.5-air 把谐音放行，
        还把正常昵称「阿澈」误杀；同批用例 4.7 全对。
      · 调用失败 / 超时 / 返回看不懂 → 返回 None，由调用方**放行**。
        不能因为审核服务不可用就把起名整个堵死，何况本地词表还在兜底。
    """
    try:
        out = await call_model(
            [
                {"role": "system", "content": prompts.PROMPT_NICK_JUDGE},
                {"role": "user", "content": f"称呼：{nick}"},
            ],
            # 让它先说一句依据再落结论（思路摊开之后更准，漏判也留得下解释），
            # 所以这里给到 160 —— 相比一次漏判的代价，这点 token 不值一提。
            max_tokens=160,
            tier="judge",
            temperature=0.0,       # 判断类必须冻住随机性，见 call_model 的说明
        )
    except Exception as e:
        logger.warning("🕵️ 称呼审核没跑通（先放行）: %s", str(e)[:120])
        return None
    raw = (out or "").strip()
    if not raw:
        return None
    # 取**最后一处**结论：正文里提到「OK」之类的字样不该影响判决，结论永远在末行。
    picks = re.findall(r"(?:^|\s)(OK|NG)(?:\s|$)", raw.upper())
    decision = picks[-1] if picks else ""
    if decision == "NG":
        logger.info("🕵️ 称呼审核理由（NG）：%s", raw.replace("\n", " ")[:160])
        return False
    if decision == "OK":
        return True
    logger.warning("🕵️ 称呼审核返回看不懂的内容，先放行: %r", raw[:60])
    return None


async def ask_digest(system, user):
    """摘要走**压缩梯队**（便宜、能吞原始聊天），输出上限单独配（比回复长得多）。

    不用对话梯队：压缩的 prompt 与聊天不共享前缀（换模型零缓存损失）、后台跑不怕慢、
    也不参与人格。且实测对话梯队里最强的那档更容易被内容过滤拒掉。
    """
    out = await call_model(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        config.DIGEST_MAX_TOKENS, tier="digest",
    )
    # 出口再做一次**确定性**清洗。提示词里已经写了「敏感内容不复述」的硬规则，但实测拦不住：
    # 同一份含词的真实记录，加规则前后输出命中的词数一处没少（模型当普通昵称照抄了）。
    # 所以这里不指望模型的判断力，直接按词表把命中的词换掉。
    # 摘要正文和 <DIGEST> 结构块同源（都来自这一段 raw），洗 raw 就等于全覆盖。
    return wordfilter.scrub(out)


async def run_digest(group_id, reason=""):
    """压缩一个群的长期记忆，产出摘要 + 当天 MD + 更新承诺账本。

    同一群串行，避免定时任务和手动触发撞车。
    关键：**只有压缩成功才推进 last_run**，失败时消息仍留在归档里，下一轮会重读。
    """
    lock = _digest_locks.setdefault(group_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        entries = ARCHIVE.since(group_id, DIGESTS.last_run(group_id),
                                limit=config.DIGEST_MAX_PENDING)
        if not entries:
            return
        logger.info("🗜️  开始压缩群 %s 的长期记忆（%d 条待处理 / %s）",
                    group_id[-6:], len(entries), reason or "手动")
        started = time.time()
        try:
            result = await digest_mod.compress_group(
                DIGESTS, group_id, entries, ask_digest,
                resolver=_resolve_name(group_id),
                budget=BUDGET.try_consume,
                log=logger.info,
                refresh=lambda t: refresh_names(group_id, t),
            )
        except Exception as e:
            logger.error("❌ 群记忆压缩异常: %s", str(e)[:200])
            return
        if not result:
            logger.warning("⚠️ 群 %s 记忆未产出结果，原文保留在归档里，下轮再试", group_id[-6:])
            return

        DIGESTS.mark_compressed(group_id)
        STATE.mark_dirty()
        logger.info("📜 群 %s 记忆已更新（%.1fs / %d 条）：%s",
                    group_id[-6:], time.time() - started, len(entries),
                    (result.get("brief") or "")[:70])

        # 承诺账本跟着摘要对齐：新出现的记上，摘要里消失的自动撤下
        if config.PROMISE_ENABLED:
            added, dropped = PROMISES.sync(group_id, (result.get("data") or {}).get("promises"))
            if added or dropped:
                STATE.mark_dirty()
                logger.info("🧾 承诺账本已同步：新增 %d 条，撤下 %d 条", added, dropped)

        # 好感度结算：这一整段时间里谁更亲近、谁在抬杠，一次性落库
        # （每轮不再让模型自评，见 config.CMD_PROTOCOL_ENABLED）
        if config.AFFINITY_ENABLED:
            n_aff, detail = apply_digest_affinity(
                group_id, (result.get("data") or {}).get("affinity"))
            if n_aff:
                logger.info("💗 压缩结算好感度：%d 人（%s）", n_aff, detail)
            else:
                logger.info("💗 压缩结算好感度：本段时间没有人的态度明显变化")

        # 当天的人话版 MD，平时看这个就够，出错再回 archive/ 查原文
        if config.DIGEST_WRITE_MD:
            try:
                path = ARCHIVE.write_memory(
                    group_id, result.get("brief") or "", result.get("data") or {},
                    meta={"本次压缩消息数": len(entries), "耗时": f"{time.time() - started:.1f}s"},
                )
                logger.info("📝 已写入 %s", os.path.relpath(path, config.BASE_DIR))
            except Exception as e:
                logger.warning("⚠️ 写入记忆 MD 失败: %s", str(e)[:120])


async def digest_loop():
    """后台定时扫描：到点就压，归档里攒太多也立即压。"""
    while True:
        await asyncio.sleep(config.DIGEST_CHECK_INTERVAL)
        if not config.DIGEST_ENABLED:
            continue
        try:
            for gid in list(DIGESTS.groups.keys()):
                pending = len(ARCHIVE.since(gid, DIGESTS.last_run(gid)))
                if not DIGESTS.needs_compress(
                        gid, config.DIGEST_INTERVAL_HOURS, config.DIGEST_MIN_PENDING,
                        pending, count_trigger=config.DIGEST_COUNT_TRIGGER):
                    continue
                await run_digest(gid, reason=DIGESTS.compress_reason(
                    gid, config.DIGEST_INTERVAL_HOURS, pending,
                    count_trigger=config.DIGEST_COUNT_TRIGGER))
        except Exception as e:
            logger.error("⚠️ 群记忆调度异常: %s", str(e)[:200])


# ── 承诺催债 ──

def quiet_hour():
    return time.localtime().tm_hour in config.PROMISE_QUIET_HOURS


async def post_to_group(group_id, text):
    """主动往群里发一句话（不是回复）。催债、定时播报都要用它。"""
    client = _bot_ref.get("client")
    if client is None or not text:
        return False
    try:
        await client.api.post_group_message(group_openid=group_id, content=text, msg_type=0)
        logger.info("📢 [主动发言] 群 %s | %s", group_id[-6:], text.replace("\n", " ")[:150])
        return True
    except Exception as e:
        logger.error("❌ 主动发言失败: %s", str(e)[:150])
        return False


async def dun_promises(group_id):
    """挑一条最该催的承诺，让它开口要账。一天最多一次，催满上限就撤下。"""
    dunnable = PROMISES.dunnable(group_id)
    if not dunnable:
        return
    row = dunnable[0]
    if not BUDGET.try_consume():
        logger.warning("🛑 今日额度已用尽，本次催债跳过（%d/%d）", BUDGET.used, BUDGET.limit)
        return
    STATE.mark_dirty()
    lines = []
    for r in PROMISES.list(group_id):
        due = r.get("due_text") or "没说时间"
        lines.append(f"- {r.get('who','有人')}：{r.get('what','')}（说的时间：{due}）")
    # 和长期记忆一样，账本里存的是**当时**那个称呼。不刷新的话，人改了名，
    # 催债的话还是会用旧名叫人 —— 而且这段是直接进 prompt 的。
    payload = refresh_names(group_id, "未兑现清单：\n" + "\n".join(lines))
    text = await call_model(
        [{"role": "system", "content": naming.render(digest_mod.SYSTEM_DUN)},
         {"role": "user", "content": payload}],
        config.AI_MAX_TOKENS_BANTER,
    )
    if not text:
        return
    text = text.strip().strip('"')
    if not await post_to_group(group_id, text):
        return
    nag = PROMISES.mark_nagged(group_id, row["id"])
    STATE.mark_dirty()
    logger.info("🧾 已催债（第 %d 次）：%s —— %s", nag, row.get("who"), row.get("what"))


async def promise_loop():
    """后台催债：每小时看一次，只在白天、且群里当天有人说话时才开口。"""
    while True:
        await asyncio.sleep(config.PROMISE_CHECK_INTERVAL)
        if not config.PROMISE_ENABLED:
            continue
        try:
            if quiet_hour():
                continue
            for gid in list(PROMISES.items.keys()):
                # 群里今天都没人说话，就没必要自顾自地开口
                seen = (STATE.data.get("groups") or {}).get(gid, {}).get("last_seen", 0)
                if time.time() - seen > 86400:
                    continue
                await dun_promises(gid)
        except Exception as e:
            logger.error("⚠️ 催债调度异常: %s", str(e)[:200])


def apply_relation_delta(group_id, member_openid, delta, reason=""):
    """把一次互动的情感分值写进档案。

    默认情况下这里是**本地关键词兜底**在打分（见下方调用点）—— 让模型每轮自评那套已经撤了，
    好感度的模型判断挪到每日压缩里做（apply_digest_affinity）。若哪天把
    config.CMD_PROTOCOL_ENABLED 打开回退，这里又会优先用模型给的分值。
    """
    if not config.AFFINITY_ENABLED:
        return None
    score, old_lv, new_lv = RELATIONS.apply(group_id, member_openid, delta)
    STATE.mark_dirty()
    logger.info("💗 [%s/%s] %+d → %d 档位=%s%s %s",
                group_id[-6:], member_openid[-6:], delta, score, new_lv,
                f"（原{old_lv}）" if old_lv != new_lv else "", reason)
    return score


def apply_digest_affinity(group_id, rows):
    """把每日压缩结算出来的「对某人的印象变化」写进关系档案。

    为什么从每轮挪到这儿：压缩看得到一整天完整的对话，判得比让模型每轮顺手自评准；
    而且不用再逼它每轮多吐一行 JSON，注意力能全留给角色扮演。
    返回 (落库人数, 明细文本)。
    """
    if not config.AFFINITY_ENABLED or not rows:
        return 0, ""
    # 摘要里写的是人话（「阿强」），落库要的是 openid，这里反查。
    # ⚠️ 反查是**单向**的：模型给的名字只用来找 openid，永远不回写 rec["nick"]。
    # 主名只能由本人或群主授权来定（见 relations.MAIN_NAME_SOURCES）——摘要写的名字
    # 即便看着更像样，也只能落到「印象变化」上，动不了称呼本身。
    by_nick = {}
    for oid, rec in _all_named(group_id):
        nick = (rec.get("nick") or "").strip()
        if nick:
            by_nick.setdefault(nick, oid)
    applied, notes = 0, []
    for row in rows:
        if not isinstance(row, dict):
            continue
        who = str(row.get("who") or "").strip()
        target = by_nick.get(who)
        if not target:
            # 名字对不上（改过名，或摘要写了个没人认领过的称呼）：宁可不记，也不能记错人
            continue
        try:
            delta = int(row.get("delta"))
        except (TypeError, ValueError):
            continue
        if not delta:
            continue
        score, old_lv, new_lv = RELATIONS.apply(
            group_id, target, delta,
            span=config.AFFINITY_DIGEST_SPAN, count=False,
        )
        applied += 1
        notes.append(f"{who}{delta:+d}→{score}")
    if applied:
        STATE.mark_dirty()
    return applied, "、".join(notes)


# ══════════════════════ 7. 机器人主体 ══════════════════════


class GroupBot(botpy.Client):
    async def on_ready(self):
        _bot_ref["client"] = self
        # 平台昵称登记进 naming：它是「附带别名」，不是触发词的权威来源 ——
        # 只会往 bot_names() 里**多加**一个叫法，顶不掉 .env 的 BOT_NAMES；
        # 而且登录时读一次，改了 QQ 昵称要重启进程才会生效。
        naming.set_platform_name(getattr(self.robot, "name", ""))
        logger.info("=" * 50)
        logger.info("🎉 机器人「%s」(ID: %s) 已更新上线！", self.robot.name, self.robot.id)
        logger.info("📛 它在这群里的叫法（触发词）：%s", "、".join(naming.bot_names()))
        if naming.bot_names() == [naming.DEFAULT_BOT_NAME]:
            # 触发词一个都没配、平台昵称也拿不到 —— 它现在只认「机器人」这个通用词。
            # 和「群主未认领」一样属于静默失效：喊它不应、也不主动接话，但没有任何报错。
            logger.warning(
                "📛 没配触发词、也没拿到平台昵称 —— 它现在只认「%s」这个通用词，"
                "群里喊别的名字它是不会应的。在 .env 里设 BOT_NAMES（可多个别名），"
                "或直接在 QQ 那边给它改个昵称",
                naming.DEFAULT_BOT_NAME)
        if OWNER_OPENID:
            logger.info("👑 群主 OpenID 已锁定: %s（好感度锁定顶格；称呼认领后自动生效）", OWNER_OPENID)
        elif config.OWNER_CLAIM_PHRASE:
            # 只在用的是**默认**暗号时才把它打出来 —— 自定义暗号一律不落日志。
            which = (f"现在用的是默认暗号「{config.OWNER_CLAIM_PHRASE}」，"
                     "建议改成只有你知道的一句"
                     if config.OWNER_CLAIM_PHRASE == config.DEFAULT_OWNER_CLAIM_PHRASE
                     else "用的是你在 .env 里配的自定义暗号（这里不打印）")
            logger.warning(
                "👑 还没有群主认领 —— 让群主**私聊**机器人发一句暗号即可永久认领"
                "（群里说破天也不授权，那等于谁先喊谁得）。%s；认领结果写进 %s，删掉它就能换人认领",
                which, config.OWNER_FILE)
        else:
            logger.warning("👑 还没有群主认领，而且 OWNER_CLAIM_PHRASE 是空的 —— "
                           "私聊认主通道已关闭，只能把 OpenID 手动写进 %s", config.OWNER_FILE)
        logger.info("📡 全量群消息监听与智能上下文缓存池已就绪！")
        logger.info("🤖 供应商=%s 主模型=%s | 今日额度已用 %d/%d",
                    config.PROVIDER["label"], CANDIDATE_MODELS[0], BUDGET.used, BUDGET.limit)
        logger.info("💾 状态=%s | 会话 %s | 关系档案 %s",
                    config.STATE_FILE, SESSIONS.stats(), RELATIONS.counts())
        arch = ARCHIVE.stats()
        logger.info("🗄️  原文归档=%s/ （%d 天 / %d 条）| 摘要 MD=%s/",
                    config.ARCHIVE_DIR, arch["days"], arch["lines"], config.MEMORY_DIR)
        if config.DIGEST_ENABLED:
            pending = {g: len(ARCHIVE.since(g, DIGESTS.last_run(g))) for g in DIGESTS.groups}
            logger.info("🧠 群聊长期记忆已启用 | 攒够 %d 条或每 %.0f 小时压缩一次 | 待压缩 %s",
                        config.DIGEST_COUNT_TRIGGER, config.DIGEST_INTERVAL_HOURS,
                        "、".join(f"群{k[-6:]}:{v}条" for k, v in pending.items()) or "无")
        else:
            logger.info("🧠 群聊长期记忆已关闭（DIGEST_ENABLED=false）")
        if config.PROMISE_ENABLED:
            total = sum(len(v) for v in PROMISES.items.values())
            logger.info("🧾 承诺催债已启用 | 在账 %d 条 | 宽限 %.0f 小时，最多催 %d 次",
                        total, config.PROMISE_GRACE_HOURS, config.PROMISE_MAX_NAG)
        logger.info("=" * 50)
        asyncio.create_task(probe_provider())

    async def on_group_message_create(self, message: GroupMessage):
        await self.handle_group_msg(message, is_at=False)

    async def on_group_at_message_create(self, message: GroupMessage):
        await self.handle_group_msg(message, is_at=True)

    async def handle_group_msg(self, message: GroupMessage, is_at: bool = False):
        global OWNER_OPENID

        # 1. 去重：同一条消息可能被多个事件重复投递
        if message.id in processed_msg_ids:
            return
        processed_msg_ids.append(message.id)

        # 这条消息也算「群还活着」：被让位的供应商要等群静下来才回来重试（见 _provider_available）
        _note_activity()

        raw_content = message.content or ""
        group_id = message.group_openid
        sender_openid = message.author.member_openid

        # 2. 识别 @机器人 或直接喊它的名字
        #    「要不要理这条」必须看**原始 content**：判定要趁 <@!openid> 还在的时候做。
        #    被 @ 的其他群友优先信事件体的 mentions。
        bot_id = getattr(self.robot, "id", "")
        bot_name = getattr(self.robot, "name", "")
        mentioned_others = extract_mentions(message, bot_id)
        if mentioned_others:
            logger.info("🔗 捕捉到艾特对象 %s", "、".join(m[-4:] for m in mentioned_others))
            # 事件体只给 openid，正文里留下的明文 @昵称 是唯一能知道「群里怎么
            # 显示他」的地方 —— 日常互相 @ 是大头，别只认群主命名那一种。
            if learn_display_names(
                    group_id, mentioned_others, raw_content, naming.bot_names()):
                STATE.mark_dirty()

        # 规范化时把 @ 翻成人话（@ 了谁由档案回答）—— @ 不再被清掉，见 qqtext 的说明
        user_input = qqtext.normalize(
            raw_content, mention_label=mention_label_for(group_id, bot_id, bot_name))
        # 名字全部按当前配置取，代码里不写死任何人名（见 naming.py）
        has_name_call = any(n and n in user_input for n in naming.bot_names())
        is_at = is_at or ("<@" in raw_content) or has_name_call
        if is_at:
            user_input = _strip_call(user_input)

        # 读不出人话的消息（纯表情/纯图片/纯链接）到这里就没了。整条丢弃：
        # 不入库、不进背景缓存、不回复。只有「@了机器人却一个字没说」例外，
        # 那还欠他一句「找我啥事」。
        if not user_input and not is_at:
            return

        logger.info("📩 [%s] %s | %s", "@艾特" if is_at else "群消息", sender_openid, user_input)

        # 3. 归档（这是查证用的底稿）
        ARCHIVE.append(group_id, sender_openid, user_input, at=is_at)
        if config.DIGEST_ENABLED:
            DIGESTS.touch(group_id)
        # 记一下这个群最近有人说话：主动催债前要确认群里还活着。
        # 就地更新而不是整块替换 —— 这个槽位还存着别的每群标记（比如群主认领提示有没有说过）。
        STATE.data.setdefault("groups", {}).setdefault(group_id, {})["last_seen"] = time.time()
        STATE.mark_dirty()

        # 3.5 写入群聊滑动窗口背景缓存。
        #     只挡「同一个人连着刷一模一样的内容」（复读机、表情三连），别让它把窗口占满；
        #     其余照收。以前这里是「? / 6 / 草 / 哈哈」之类的白名单枚举，来一句新的
        #     口头禅就得回来加一个词，而且把「？」这种真实的接话信号也一起挡掉了。
        buf = group_buffers[group_id]
        if not buf or buf[-1]["sender"] != sender_openid or buf[-1]["text"] != user_input:
            buf.append({"sender": sender_openid, "text": user_input, "time": time.time()})
            STATE.data["buffers"][group_id] = list(buf)

        # 4. 群主认领 —— **群里永远不授权**，这里只负责把人指到私聊去。
        #    以前这段真的会在群里认主（谁先喊谁得），等于把身份挂在大喇叭上招领；
        #    现在群里说破天也只是收到一句「去私聊办」，权限一个字都不给。
        if looks_like_claim_attempt(user_input):
            owner = owner_label(group_id)
            if OWNER_OPENID is None:
                # 每个群只正面回一次，免得有人反复喊就反复刷屏
                if claim_redirect_pending(group_id):
                    await safe_reply(message, OWNER_CLAIM_HINT)
                return
            if OWNER_OPENID == sender_openid:
                await safe_reply(message, f"{owner}，您早就认领过了 —— 您的 OpenID 已经焊死在我的"
                                          "系统核心里，有什么吩咐直接说就行。")
                return
            await safe_reply(message,
                f"（战术后仰并投来极度嫌弃的目光）\n差不多得了！真正的{owner}"
                "早就私聊跟我对完暗号了，你个山寨货还想在这儿篡位夺权呢？"
                f"信不信我现在就向{owner}打小报告，给你安排个禁言大礼包？")
            return

        is_owner = OWNER_OPENID is not None and sender_openid == OWNER_OPENID

        # 群模式（顺便把旧的 cadre 存档迁移成 discipline）
        current_mode = resolve_mode(STATE.get_mode(group_id))
        if current_mode != STATE.get_mode(group_id):
            STATE.set_mode(group_id, current_mode)

        # 5.4 起名开关：群主一句话锁上/解开整个群的起名。0 token，纯本地。
        #     排在昵称指令解析之前 —— 「锁起名」本身不含「叫我/叫X」，不会撞，
        #     但万一撞了也要让开关先说话。
        lock_cmd = relations.parse_nick_lock_command(user_input)
        if lock_cmd:
            await handle_nick_lock(message, group_id, lock_cmd, is_owner)
            return

        # 5.5 昵称管理：本人随时认领或改（以最新一次为准），群主可以 @某人 给对方指定
        #     「机器人名，叫 小满」这种带称呼前缀的要先剥掉前缀才认得出来
        bot_names = naming.bot_names()
        # mentioned_others 照传：它是「这人 @ 了别人」的事实，剥 @ 文本要用到，
        # 否则「@阿澈 叫我阿强」连本人改名都会失效。能不能给别人起名是另一件事，
        # 交给 allow_other 控制（只有群主才可能是在给别人起名）。
        nick_cmd = relations.parse_nick_command(
            user_input, bot_names=bot_names,
            mentioned_others=mentioned_others, allow_other=is_owner)
        if nick_cmd:
            await handle_nick_command(message, nick_cmd, group_id, sender_openid,
                                      is_owner, mentioned_others)
            return
        # 解析不出命令 ≠ 没有这个意图：普通群友的「叫@某人 X」被 allow_other 吞掉了，
        # 静默掉进闲聊会让人以为在商量、甚至以为改成了。带动词的是明确意图，必须给说法。
        # 只认带动词的（不认「@老王 你好」那种任意短句），否则每句打招呼都要被回绝一次。
        if not is_owner and relations.looks_like_other_nick(
                user_input, bot_names=bot_names, mentioned_others=mentioned_others):
            logger.info("🚫 非群主要给别人起名，已明说（%s）", sender_openid[-4:])
            await safe_reply(message, NICK_OTHER_DENIED.format(owner=owner_label(group_id)))
            return

        # 6. 群主专属特权指令
        if is_owner and any(k in user_input for k in OWNER_COMMANDS):
            await safe_reply(message,
                "（唰地拔出四十米纯钛合金绣春刀，单膝跪地抱拳）\n"
                f"锦衣卫{naming.bot_name()}领旨！大胆刁民，竟敢触犯天颜！\n"
                f"本卫已将该狂徒打入《群聊诛九族大牢》，剥夺摸鱼政治权利终身！{owner_label(group_id)}，"
                "您看是直接拖去午门斩首，还是没收其键盘三年？小的立刻去办！")
            return

        # 6. 极速本地彩蛋：赛博决斗（免@ 0 延迟，结果也计入交情）
        if any(k in user_input for k in DICE_TRIGGERS):
            await reply_duel(message, group_id, sender_openid)
            return

        # 7. 模式切换（免@生效，纯本地文案，不消耗 AI）
        if any(k in user_input for k in exit_triggers()):
            if current_mode != "normal":
                STATE.set_mode(group_id, "normal")
                _reset_style(group_id, current_mode, "normal")
                line = MODE_EXIT_LINES.get(current_mode, "收到！已恢复普通{bot}模式！")
                await safe_reply(message, naming.render(line, owner=owner_label(group_id)))
                return

        for mode_name, triggers, entry_line in MODE_ENTRIES:
            if current_mode != mode_name and any(k in user_input for k in triggers):
                STATE.set_mode(group_id, mode_name)
                _reset_style(group_id, current_mode, mode_name)
                await safe_reply(message, naming.render(entry_line, owner=owner_label(group_id)))
                return

        # 8. 本地极速梗彩蛋（0 延迟 0 配额消耗）
        for meme_key, meme_value in LOCAL_MEMES.items():
            if meme_key in user_input and not is_at:
                now = time.time()
                if now - group_last_random_reply.get(group_id, 0) >= config.MEME_COOLDOWN_SECONDS:
                    if random.random() < config.MEME_PROBABILITY:
                        group_last_random_reply[group_id] = now
                        STATE.data["cooldowns"][group_id] = now
                        STATE.mark_dirty()
                        await safe_reply(message, meme_value)
                        return

        # 8.5 关系档案本地查询（直接读档，0 token，不打扰模型）
        if any(k in user_input for k in RELATION_QUERY_TRIGGERS):
            rec = RELATIONS.get(group_id, sender_openid)
            await safe_reply(message, relations.render_status(rec, sender_openid, mode=current_mode))
            return

        if any(k in user_input for k in RELATION_BOARD_TRIGGERS):
            rows = RELATIONS.rank(group_id, topn=config.AFFINITY_BOARD_SIZE)
            await safe_reply(message, relations.render_rank(rows, mode=current_mode))
            return

        # 名字绑错人时用它当场核对：谁的名字挂在哪个人头上
        if any(k in user_input for k in NAME_TABLE_TRIGGERS):
            rows = _all_named(group_id)
            await safe_reply(message, relations.render_name_table(rows, mode=current_mode))
            return

        if config.DIGEST_ENABLED and any(k in user_input for k in DIGEST_TRIGGERS):
            await safe_reply(message, render_digest_reply(group_id, current_mode))
            return

        # 8.7 承诺台账：查账是本地读取，结清按昵称匹配撤下
        if config.PROMISE_ENABLED:
            if any(k in user_input for k in PROMISE_CLEAR_TRIGGERS):
                rec = RELATIONS.get(group_id, sender_openid)
                nick = (rec or {}).get("nick") or ""
                removed = PROMISES.resolve(group_id, keyword=nick or sender_openid[-4:])
                if removed:
                    STATE.mark_dirty()
                    await safe_reply(message,
                        f"（在小本本上重重划掉 {removed} 行）行，算你说话算话，这笔账销了。")
                else:
                    await safe_reply(message, "（翻遍台账）你名下好像没欠着什么啊，别急着邀功。")
                return

            if any(k in user_input for k in PROMISE_TRIGGERS):
                # 同上：发到群里之前先刷新，别拿旧名当众叫人
                await safe_reply(message, refresh_names(group_id, PROMISES.render(group_id)))
                return

        # 8.8 回原文查证：摘要出错时用它翻底稿
        m_lookup = RE_LOOKUP.match(user_input)
        if m_lookup:
            hits = ARCHIVE.search(group_id, m_lookup.group(1), limit=8)
            if not hits:
                await safe_reply(message, f"（翻遍归档）没找着含「{m_lookup.group(1)}」的发言，"
                                          "要么你记错了，要么这事只在你脑子里发生过。")
            else:
                lines = []
                for h in hits[-8:]:
                    when = time.strftime("%m-%d %H:%M", time.localtime(h["ts"]))
                    # 翻旧账是**发出去给人看的**：称呼 > 显示名 > 泛称，不吐 openid
                    who = (_resolve_name(group_id)(h["sender"])
                           or DISPLAY_NAMES.of(group_id, h["sender"])
                           or "某位群友")
                    lines.append(f"[{when}] {who}：{h['text'][:60]}")
                await safe_reply(message,
                    f"🔍 【翻旧账·{m_lookup.group(1)}】共 {len(hits)} 条，最近这些：\n" + "\n".join(lines))
            return

        # 8.9 手动压一次：不用等定时器，方便立刻验证效果
        if config.DIGEST_ENABLED and any(k in user_input for k in MANUAL_DIGEST_TRIGGERS):
            await safe_reply(message, "（摊开小本本，把最近的聊天从头捋一遍）稍等，我理一理。")
            await run_digest(group_id, reason="手动触发")
            await safe_reply(message, render_digest_reply(group_id, current_mode))
            return

        # 9. 判定这条要不要接
        is_fortune = any(k in user_input for k in FORTUNE_TRIGGERS)

        should_reply = False
        is_random = False
        if is_at or is_fortune:
            should_reply = True
        else:
            # 不做文字长度门槛：群里一句「？」「太蠢了」就是真实的接话信号，
            # 拿字数当尺子只会把能接的挡在外面。频率交给概率和冷却控制。
            # （读不出内容的消息在入口就被丢掉了，到这里的都是人话。）
            now = time.time()
            if now - group_last_random_reply.get(group_id, 0) >= config.BANTER_COOLDOWN_SECONDS:
                # 聊到群主时更容易被点着：这是群里最现成的梗源，也是它刷存在感最自然的位置。
                # 判断「是不是在说群主」不看写死的人名 —— 见 owner_reference_terms。
                if mentions_owner(user_input, group_id, mentioned_others):
                    chance = config.OWNER_MENTION_PROBABILITY
                else:
                    chance = config.BANTER_PROBABILITY
                if random.random() < chance:
                    should_reply = True
                    is_random = True
                    group_last_random_reply[group_id] = now
                    STATE.data["cooldowns"][group_id] = now
                    STATE.mark_dirty()

        if not should_reply:
            return

        if is_at and not user_input:
            me = naming.bot_name()
            await safe_reply(message, f"找{me}啥事？直接说，{me}在线接单！")
            return

        # 10. 两道闸：群级令牌桶 + 全局日预算
        if not await BudgetGate.check(message, group_id):
            return

        # 11. 组装群聊背景 + session，走 AI
        context_hint = ""
        buf = group_buffers.get(group_id)
        if buf and len(buf) > 1:
            prev = [b for b in list(buf)[:-1] if b["text"] != user_input][-3:]
            if prev:
                # 必须标出发言人。之前只给一串裸文本，模型分不清哪句是谁说的，
                # 于是把别人认领的名字安到了当前这位头上 —— 叫错人就是这么来的
                def _who(oid):
                    # 认领过的称呼 > 群里挂的显示名 > 泛称。以前这里拼 openid 后四位，
                    # 结果模型把它当成名字复读进了回复 —— 群里没人看得懂。
                    r = RELATIONS.get(group_id, oid, create=False)
                    return ((r or {}).get("nick")
                            or DISPLAY_NAMES.of(group_id, oid)
                            or "一位群友")

                context_hint = refresh_names(
                    group_id, "\n".join(f"- {_who(b['sender'])}：{b['text']}" for b in prev))
                context_hint = ("（注意：这是别人说过的话，标了发言人，别张冠李戴；"
                                "也不要刻意复读）\n" + context_hint)

        active_mode = current_mode
        if is_fortune and current_mode == "normal":
            active_mode = "fortune"

        # 每人每群一份独立 session，彻底隔离会话，绝不串台
        session_id = f"{group_id}_{sender_openid}"

        # 关系档案注入：告诉模型「对面这个人跟你有过多深的往来」，同时消费掉待播报的里程碑
        relation_note = ""
        target_record = None
        if config.AFFINITY_ENABLED:
            target_record = RELATIONS.get(group_id, sender_openid)
            relation_note = relations.build_relation_note(
                target_record, sender_openid, mode=active_mode,
                other_names=_other_names(group_id, sender_openid),
            )
            if target_record.get("pending_milestone"):
                target_record["pending_milestone"] = None
                STATE.mark_dirty()

        # 群聊长期记忆：有就注入，让它可以说「上周撺掇打牌那事儿我可还记着」
        group_memory = ""
        if config.DIGEST_ENABLED:
            summary = DIGESTS.get(group_id)
            if summary and (summary.get("brief") or (summary.get("data") or {}).get("topics")):
                # 名字统一刷新成当前称呼再注入：模型永远拿不到旧名，也就编不出
                # 「那谁和这谁」这种把同一个人拆成两个人的往事。
                group_memory = refresh_names(
                    group_id, digest_mod.render_summary(summary, mode=active_mode))
                group_memory = (
                    "【这个群最近发生的事（系统整理的长期记忆，真实发生过，可以拿来接梗、催债、点名，"
                    "但不要编造记忆里没有的内容）】\n" + group_memory
                )

        reply_text, delta = await get_ai_reply(
            session_id, user_input,
            is_owner=is_owner, mode=active_mode,
            context_hint=context_hint, is_random_banter=is_random,
            relation_note=relation_note, group_memory=group_memory,
            owner_label=owner_label(group_id),
        )
        if owner_hint_pending(group_id):
            reply_text = f"{reply_text}\n{OWNER_CLAIM_HINT}"
        await safe_reply(message, reply_text)

        # 日内微调：模型不再为每条回复打分（那活儿挪到每日压缩了），这里默认拿到的是
        # 本地关键词的 ±1，只负责「当天就有点反馈」；真正的印象结算在压缩里做。
        if config.AFFINITY_ENABLED:
            if delta is None:
                delta = relations.local_sentiment(user_input)
            apply_relation_delta(group_id, sender_openid, delta, "AI互动")

    async def on_c2c_message_create(self, message: Message):
        """私聊。以前这里连去重都没有，重复投递会重复扣额度。"""
        global OWNER_OPENID
        if message.id in processed_msg_ids:
            return
        processed_msg_ids.append(message.id)

        user_input = (message.content or "").strip()
        sender_openid = message.author.user_openid
        logger.info("📩 [私聊] %s | %s", sender_openid, user_input)
        if not user_input:
            return

        # 认主：**只在私聊，且必须说对暗号**。第一个说对的 openid 被永久写进 owner.txt。
        # 认领之后就失效了（不做转移）—— 所以口令将来就算泄了，也抢不走这个位置。
        if matches_claim_phrase(user_input):
            if OWNER_OPENID is None:
                OWNER_OPENID = sender_openid
                save_owner(OWNER_OPENID)
                RELATIONS.pin(OWNER_OPENID)   # 好感度钉在顶格：亲近靠档案，不靠谄媚台词
                STATE.mark_dirty()
                logger.info("👑 【认主成功】已永久锁定唯一群主 OpenID: %s（好感度锁定顶格）",
                            OWNER_OPENID)
                await safe_reply(message,
                    "（当场立正敬礼，掏出纯金VIP打卡机录入指纹）\n滴！认主成功！"
                    f"从今往后{naming.bot_name()}唯您马首是瞻 ——"
                    "给别人起名、特权指令这些，往后只有您说了算。")
                return
            if OWNER_OPENID == sender_openid:
                await safe_reply(message,
                    "您早就认领过了 —— 您的 OpenID 已经焊死在我的系统核心里，不用再对一次。")
                return
            # 已经有主了。**不透露是谁**：只报「有人认领过」，一个字都不多说。
            await safe_reply(message, "（礼貌地鞠了个躬）这边已经有人认领过我了，就不另立山头了。")
            return

        # 说了句「像是要认领」的话但没对上暗号。**静默失效正是这个功能最大的坑**，所以明说，
        # 而且这句是本地固定话术：0 token，也不占每日额度。
        if OWNER_OPENID is None and looks_like_claim_attempt(user_input):
            if config.OWNER_CLAIM_PHRASE:
                await safe_reply(message,
                    "这是想认领我？那得说对暗号才行 —— 暗号是部署的时候在 `OWNER_CLAIM_PHRASE` "
                    "里配的那一句。")
            else:
                await safe_reply(message,
                    "想认领我？这台机器的私聊认主通道是关着的 —— 部署的人没有配 "
                    "`OWNER_CLAIM_PHRASE`。")
            return

        is_owner = OWNER_OPENID is not None and sender_openid == OWNER_OPENID

        # 私聊不办改名：称呼是按群存的（`群号|openid`），这条消息里没有群号 ——
        # 改了也不知道该改在哪儿。必须**明说**：以前这句话直接喂给模型，被当成闲聊
        # 回了个玩笑，当事人完全不知道命令没生效，还以为「叫不动它」。0 token，不占额度。
        nick_cmd = relations.parse_nick_command(
            user_input, bot_names=naming.bot_names(),
            mentioned_others=(), allow_other=is_owner)
        if nick_cmd:
            logger.info("🚫 私聊收到改名命令（%s），已指回群里", nick_cmd.get("scope"))
            await safe_reply(message, NICK_PRIVATE_CHAT_HINT)
            return

        # 起名开关同理：锁是按群存在的，私聊没有群号，指回群里办
        if relations.parse_nick_lock_command(user_input):
            logger.info("🚫 私聊收到起名开关指令，已指回群里")
            await safe_reply(message, NICK_PRIVATE_CHAT_HINT)
            return

        if not BUDGET.try_consume():
            logger.warning("🛑 今日额度已用尽，私聊也一并拒绝（%d/%d）", BUDGET.used, BUDGET.limit)
            STATE.mark_dirty()
            await safe_reply(message, "（小声）今天额度用冒了，明天再陪你聊啊兄弟。")
            return
        STATE.mark_dirty()

        reply_text, _delta = await get_ai_reply(
            f"user_{sender_openid}", user_input, is_owner=is_owner, mode="normal",
            private=True,
        )
        await safe_reply(message, reply_text)


# ══════════════════════ 8. 启动 / 优雅退出 / 重连 ══════════════════════


def install_signal_handlers():
    """SIGTERM/SIGINT 时先落盘再退出，别让刚聊的上下文白丢。"""

    def handler(signum, _frame):
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.warning("🛑 收到 %s，正在保存状态后退出...", name)
        try:
            STATE.save_now(force=True)
            logger.info("💾 状态已保存至 %s", config.STATE_FILE)
        except Exception as e:
            logger.error("❌ 退出时保存状态失败: %s", e)
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except ValueError:
            pass  # 非主线程时无法注册，忽略


async def prune_loop():
    """定期清理过期会话，避免长期运行下内存无界增长。"""
    while True:
        await asyncio.sleep(config.SESSION_PRUNE_INTERVAL)
        try:
            dropped = SESSIONS.prune()
            if dropped:
                STATE.mark_dirty()
                logger.info("🧹 已清理 %d 个过期会话，当前 %s", dropped, SESSIONS.stats())
            stale = RELATIONS.prune()
            if stale:
                STATE.mark_dirty()
                logger.info("🧹 已清理 %d 份长期失联的关系档案，当前 %s", stale, RELATIONS.counts())
        except Exception as e:
            logger.error("⚠️ 会话清理异常: %s", e)


def run_bot():
    errors = config.validate()
    if errors:
        raise SystemExit("❌ 配置缺失：\n  - " + "\n  - ".join(errors))

    setup_logging()
    patch_aiohttp_ssl()
    load_owner()

    logger.info("🤖 供应商=%s | 主模型=%s | Key 数=%d",
                config.PROVIDER["label"], CANDIDATE_MODELS[0], len(config.AI_KEYS))
    if SESSIONS._sessions:
        logger.info("💾 已恢复 %s 个会话上下文", len(SESSIONS._sessions))

    install_signal_handlers()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(STATE.flush_loop())
    loop.create_task(prune_loop())
    if config.DIGEST_ENABLED:
        loop.create_task(digest_loop())
    if config.PROMISE_ENABLED:
        loop.create_task(promise_loop())

    attempt = 0
    while True:
        started_at = time.time()
        try:
            intents = botpy.Intents(public_messages=True, public_guild_messages=True)
            client = GroupBot(intents=intents)
            client.run(appid=config.QQ_APP_ID, secret=config.QQ_APP_SECRET)
            attempt = 0
        except SystemExit:
            raise
        except Exception as e:
            attempt += 1
            logger.error("⚠️ 机器人连接异常中断: %s", str(e)[:200])
        except KeyboardInterrupt:
            logger.warning("🛑 收到 Ctrl+C")
            raise
        finally:
            try:
                STATE.save_now(force=True)
            except Exception as e:
                logger.error("❌ 落盘失败: %s", e)

        # 指数退避 + 抖动：连续失败越拖越长，稳定跑一段就把计数归零
        uptime = time.time() - started_at
        if uptime > config.RESET_BACKOFF_AFTER:
            attempt = 0
            delay = config.RECONNECT_MIN_DELAY
        else:
            delay = min(config.RECONNECT_MAX_DELAY, config.RECONNECT_MIN_DELAY * (2 ** max(0, attempt - 1)))
            delay *= random.uniform(0.8, 1.2)
        logger.info("🔁 %.1f 秒后重连（第 %d 次尝试）", delay, attempt + 1)
        time.sleep(delay)


if __name__ == "__main__":
    try:
        run_bot()
    finally:
        try:
            STATE.save_now(force=True)
        except Exception:
            pass
