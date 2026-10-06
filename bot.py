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

# ══════════════════════ 1.5 网关看门狗 ══════════════════════
#
# ⚠️ botpy 的重连链路有个盲区：掉线（1006）后重不重连，取决于 `ws_connect` 的接收循环
# 能不能正常退出。如果 TCP 半死（对端静默掉线、没有 close 帧到达），`await receive()` 会
# 永远挂住——不报错、不重连、心跳任务也各自闭嘴，整条链就这么干等。
# 实测（2026-09-19 13:15:47）：1006 后 **18 分钟**没有任何重连尝试，期间一个日志都没有。
#
# 解法：包一层 `_is_system_event`（botpy 每收到一条下行都会经过它，心跳 ACK 也不例外），
# 记下「最后一次网关动静」的时刻；再看门狗协程定期巡检，静默超过阈值就**主动踢一刀**——
# 关掉当前 ws，让接收循环拿到 CLOSED 退出，botpy 自己的重连链路随即接手。
# 心跳每 30s 一次，正常情况下静默永远不会超过一分钟。

_ws_watch = {"last_rx": 0.0, "conn": None, "kicks": 0}


def install_ws_watchdog():
    """挂上「网关最后一响」的记录钩子。必须在 client.run() 之前调用一次。"""
    from botpy.gateway import BotWebSocket

    original = BotWebSocket._is_system_event

    async def patched(self, message_event, ws):
        _ws_watch["last_rx"] = time.time()
        _ws_watch["conn"] = ws
        return await original(self, message_event, ws)

    BotWebSocket._is_system_event = patched
    _ws_watch["last_rx"] = time.time()
    logger.info("🐕 网关看门狗已挂上（静默 %.0f 秒即踢一刀重连）",
                config.WS_WATCHDOG_STALE_SECONDS)


async def _ws_watchdog_tick():
    """看门狗巡检一轮。返回 True = 这次踢了。独立成函数是为了好测。"""
    conn = _ws_watch.get("conn")
    if not _ws_watch["last_rx"]:
        return False
    silent = time.time() - _ws_watch["last_rx"]
    if silent <= config.WS_WATCHDOG_STALE_SECONDS:
        return False
    # 先记账再动手：不管踢没踢成都重置计时，防止重连进行中被连环误踢
    _ws_watch["last_rx"] = time.time()
    if conn is None or conn.closed:
        # 连接已经是死的：重连链路理应正在跑，别再补刀，等新连接的下一响
        logger.warning("🐾 看门狗：网关静默 %.0f 秒（连接已关闭，等重连链路接手）", silent)
        return False
    _ws_watch["kicks"] += 1
    logger.warning("🐾 看门狗：网关 %.0f 秒没有任何下行（心跳 ACK 也没来），"
                   "主动断开让它重连（第 %d 次）", silent, _ws_watch["kicks"])
    await conn.close(code=4000)
    return True


async def ws_watchdog_loop():
    """看门狗主循环：每 15 秒巡检一次。"""
    await asyncio.sleep(config.WS_WATCHDOG_STALE_SECONDS)  # 先给首连留足时间
    while True:
        try:
            await _ws_watchdog_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("❌ 看门狗巡检异常（不影响主流程）: %s", e)
        await asyncio.sleep(15)



# ══════════════ 领域模块（2026-10-06 拆分；以下是兼容别名） ══════════════
# 运行时单例：runtime.py │ 供应商容错：providers.py │ 好感度结算：affinity.py │ 昵称域：nicking.py
# bot.py 本体只留「消息流 + 指令路由 + 组装」。别名让既有引用与测试完全兼容。
import runtime
from runtime import hits, owner_label, load_owner, save_owner, _all_named
from providers import (PROVIDER_CHAIN, AI_CLIENTS, MODEL_CHAINS, MODEL_CHAINS_DIGEST,
                       MODEL_CHAINS_JUDGE, _TIER_CHAINS, _provider_state, _key_idx,
                       _slow_until, _activity, _HARD_FAIL_MARKS, _FATAL_FAIL_MARKS,
                       _last_msg_ts, _note_activity, _provider_available, _penalize_slow_model,
                       _model_slow, _someone_else_can_serve, _pick_last_resort,
                       _provider_serving, _provider_block_reason, _is_hard_fail,
                       _is_fatal_fail, _note_provider_fail, _note_provider_ok,
                       call_model, probe_provider)
from affinity import (apply_relation_delta, affinity_budget, apply_affinity_delta_capped,
                      apply_digest_affinity, _affinity_ledger)
from nicking import (extract_mentions, mention_nicks_from_content, mention_nick_from_content,
                     learn_display_names, mention_label_for, _strip_call, refresh_names,
                     _sync_rename, _sync_clear, _reserved_nick_names, nick_locked,
                     set_nick_locked, _taken_nick_names, reset_nick_flood,
                     _nick_block_reason, _nick_note_reject, _nick_note_accept,
                     NICK_FLOOD, _nick_rejects, _nick_cool, _nick_done,
                     _RE_PLAIN_MENTION, _RE_TRAILING_DOTS, _RE_MACHINE_ID)



group_buffers = collections.defaultdict(
    lambda: collections.deque(maxlen=config.GROUP_BUFFER_SIZE)
)
for gid, items in (runtime.STATE.data.get("buffers") or {}).items():
    group_buffers[gid] = collections.deque(items, maxlen=config.GROUP_BUFFER_SIZE)

group_last_random_reply = dict(runtime.STATE.data.get("cooldowns") or {})
processed_msg_ids = collections.deque(maxlen=config.DEDUP_MAX)


# 主动发言（催债）需要拿到 client 实例，这里存个引用
_bot_ref = {"client": None}

# 「老干部」已改为「风纪委员」。存档里如果还留着旧的 cadre，读写时自动迁移，
# 免得群里某一档模式卡在被删除的枚举值上。
LEGACY_MODE_ALIAS = {"cadre": "discipline"}

MODE_PROMPTS = prompts.MODE_PROMPTS   # 表本体在 prompts.py（人设常量的家）

FALLBACK_REPLIES = [
    "（战术后仰揉了揉眼睛）刚才走神在看隔壁单挑Boss呢，你刚才这波信息量太大，再说一遍我听着！",
    "（正在专心摸鱼中）刚才屏幕一闪没看清，哪个好兄弟又在群里发功？搞快点，再说一次听听！",
    "（反手掏出一张无懈可击）这波话题有点东西，我先喝口水冷静一下！有种你再艾特我一次，看我怎么接招！",
    "（摘下耳机假装正经）刚才走神了！你刚说啥，再艾特我一次，这回我全神贯注！",
]

_throttle_notice = {}  # group_id -> last notice timestamp






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
    if runtime.OWNER_OPENID or not group_id:
        return False
    slot = runtime.STATE.data.setdefault("groups", {}).setdefault(group_id, {})
    if slot.get("owner_hint"):
        return False
    slot["owner_hint"] = True
    runtime.STATE.mark_dirty()
    return True


def claim_redirect_pending(group_id):
    """群里有人试着认领时，要不要正面回一句「去私聊办」。每个群最多回一次。

    单独一个槽位，**不和 owner_hint 共用**：一个是「悄悄捎在回复末尾」，一个是「正面顶一句」，
    两条路都可能先被触发；共用一个标记会让另一条永远不出现。
    """
    if runtime.OWNER_OPENID or not group_id:
        return False
    slot = runtime.STATE.data.setdefault("groups", {}).setdefault(group_id, {})
    if slot.get("claim_redirect"):
        return False
    slot["claim_redirect"] = True
    runtime.STATE.mark_dirty()
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




# ══════════════════════ 4. AI 调用 ══════════════════════
































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

        # ── 2) 每日记忆 / 3) 会话历史 由 runtime.SESSIONS.build_messages 按稳定度插入 ──
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

        async with runtime.SESSIONS.lock(session_id):
            messages = runtime.SESSIONS.build_messages(
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
            runtime.SESSIONS.record(session_id, user_text, body)
            runtime.STATE.mark_dirty()
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
        ok, wait = runtime.GROUP_BUCKET.try_acquire(group_key)
        if not ok:
            logger.warning("⏳ %s 触发群级限流，%.1fs 后可再次调用", group_key, wait)
            await notify_if_quiet(
                message, "cooldown",
                "（战术后仰）慢点慢点，大伙同时开火我脑子转不过来了！容我喘口气再接招。"
            )
            return False

        if not runtime.BUDGET.try_consume():
            logger.warning("🛑 今日全局调用额度已用尽（%d/%d）", runtime.BUDGET.used, runtime.BUDGET.limit)
            await notify_if_quiet(
                message, "budget",
                naming.render("（默默关掉显示器）今天的话费额度让我用冒了，{owner}说再聊下去要卖肾了。"
                              "明天再见！", owner=owner_label(getattr(message, "group_openid", None)))
            )
            runtime.STATE.mark_dirty()
            return False

        runtime.STATE.mark_dirty()
        if runtime.BUDGET.limit and runtime.BUDGET.used >= int(runtime.BUDGET.limit * config.BUDGET_WARN_RATIO):
            logger.warning("⚠️ 今日额度已用 %d/%d（%.0f%%）", runtime.BUDGET.used, runtime.BUDGET.limit,
                           runtime.BUDGET.used / runtime.BUDGET.limit * 100)
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

# 交情不够、点单换模式被回绝时说的话（见 mode_switch_allowed）。
# ⚠️ 一个数字都不许有、也不许出现「好感度」「亲密度」—— 那等于把内部评分念给群里听。
MODE_DENIED_LINES = (
    "（歪头）诶……咱俩好像还没熟到能让我这么听话的程度吧？你先好好说话，处两天熟了再来点单～",
    "（抱臂）不是谁喊一嗓子我就变的啊，多少给点面子。先聊两天让我认认你这张脸，行不？",
    "（后退半步）慢着慢着，你哪位？这么生分就想使唤我，传出去我还要不要面子了。",
    "（假装没听见）嗯？风太大没听清。等你跟我混熟了再说这话，我考虑考虑。",
)

DICE_TRIGGERS = ["摇骰子", "掷骰子", "掷点", "比大小", "决斗", "扔骰子", "色子"]
FORTUNE_TRIGGERS = ["算一卦", "算命", "看相", "占卜", "测字"]
OWNER_COMMANDS = ["办他", "拖出去", "拿下", "拉出去", "掌嘴", "护驾"]

# 关系档案的本地查询（0 token，直接读档，不打扰模型）。
#
# ⚠️ 旧版是「好感度」「几级了」这种**裸词子串匹配**，结果是群里只要有人吐槽一句
#   「这个好感度系统没啥用」，就被抢答成一张关系卡片 —— 正经聊天被打断。
#   现在分两层：整句式的短语出现即算；「好感度」这类常见名词必须旁边带查询意图才算。
RELATION_QUERY_PHRASES = (
    "查好感", "查好感度", "看好感", "查关系", "查操行", "查亲密度",
    "我们多熟", "我跟你多熟", "我和你多熟", "咱俩多熟", "跟你多熟", "和你多熟",
    "我们熟吗", "我跟你熟吗", "咱俩熟吗", "关系怎么样", "关系如何",
    "好感多少", "操行分多少", "亲密度多少",
)
RELATION_QUERY_NOUNS = ("好感度", "亲密度", "操行分")
RELATION_QUERY_INTENTS = ("查", "看", "问", "报", "测", "显示", "多少", "怎么样", "如何",
                          "几级", "几档")


def matches_relation_query(text):
    """是不是在**问**关系/好感度 —— 光提到这个词不算。"""
    t = (text or "").strip()
    if not t:
        return False
    if any(p in t for p in RELATION_QUERY_PHRASES):
        return True
    # 「好感度」+ 查询意图（「查一下好感度」「好感度多少」）；只提这个词不算问
    return (any(n in t for n in RELATION_QUERY_NOUNS)
            and any(k in t for k in RELATION_QUERY_INTENTS))
RELATION_BOARD_TRIGGERS = ["关系榜", "群友榜", "熟人榜", "查台账", "看台账", "查考勤", "点名册"]
NAME_TABLE_TRIGGERS = ["称呼表", "名字表", "谁是谁", "改名册", "查称呼", "看称呼"]
# 群聊长期记忆（每日滚动压缩出来的《群史记》），同样是本地读取，0 token
DIGEST_TRIGGERS = ["群史记", "最近聊了啥", "群里在聊什么", "群摘要", "长期记忆", "周报"]
# 承诺台账 / 结清
PROMISE_TRIGGERS = ["查账", "承诺", "欠我", "催债", "画饼", "欠账", "谁请客"]
PROMISE_CLEAR_TRIGGERS = ["结清", "兑现了", "已兑现", "我做到了", "清账", "销账"]
# 手动触发一次压缩，不用等定时器
MANUAL_DIGEST_TRIGGERS = ["立刻总结", "马上总结", "压缩记忆", "现在总结", "生成群史记"]
# 回原文查证：翻旧账 <关键词>
RE_LOOKUP = re.compile(r"^翻旧账\s*(\S{1,20})\s*$")




def is_control_command(text):
    """这条是不是「本地控制指令」（不经过模型的那种操作）？

    模式切换、查关系、查台账、改名册、决斗……这些话是**操作**，不是聊天内容。
    不加区分地归档，它们就会在每日压缩时被当成人物事实写进长期记忆 ——
    实测事故：群主喊了句「猫娘模式」，摘要里就留下了「老王还想当猫娘」，
    还被安到了别人头上。所以这里在**归档时就打标记**，压缩源头跳过（archive.since）。
    """
    t = (text or "").strip()
    if not t:
        return False
    for family in (exit_triggers(),
                   *(triggers for _mode, triggers, _line in MODE_ENTRIES),
                   OWNER_COMMANDS, DICE_TRIGGERS, RELATION_BOARD_TRIGGERS,
                   NAME_TABLE_TRIGGERS, DIGEST_TRIGGERS, MANUAL_DIGEST_TRIGGERS):
        if hits(t, family):
            return True
    if matches_relation_query(t):
        return True
    return bool(relations.parse_nick_lock_command(t))


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




# 正文里 @ 人时留下的**明文昵称**（QQ 群消息不给昵称字段，只给 openid）。
# 切到空白或标点为止：昵称可能含空格（「A.A 贵阳老莫（全国可飞）」），
# 宁可只记第一段，也不猜边界 —— 它只是显示兜底，短一点不会出事。
























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










async def handle_nick_command(message, cmd, group_id, sender_openid, is_owner, mentioned_others):
    """昵称管理。本地词表先过一遍（0 token），可疑的再让模型看一眼（约 137 token）。

    两道是有顺序的：词表便宜且不会误伤，能拦的先在当地拦掉；
    剩下的才花 token 问模型 —— 它认得出谐音暗指，但也会偶发误判，
    所以只让它做「第二道」，不让它当第一道。
    """
    if cmd["scope"] == "self-clear":
        rec = runtime.RELATIONS.get(group_id, sender_openid)
        old = (rec.get("nick") or "").strip()
        if not old:
            await safe_reply(message, "（翻了翻小本本）你本来就没留过称呼啊，省了我一道工序。")
            return
        runtime.RELATIONS.set_nick(group_id, sender_openid, None, source="claim")
        runtime.RENAMES.note(group_id, old, sender_openid)
        _sync_clear(group_id, sender_openid, old)
        runtime.STATE.mark_dirty()
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
        old = runtime.RELATIONS.get(group_id, target, create=False)
        old_nick = (old or {}).get("nick")
        runtime.RELATIONS.set_nick(group_id, target, nick, source="owner")
        runtime.RENAMES.note(group_id, old_nick, target)
        runtime.STATE.mark_dirty()
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
            who = runtime.DISPLAY_NAMES.of(group_id, target) or "这位"
            await safe_reply(message,
                f"（工工整整把名字写进点名册）收到！往后【{who}】我就记作【{nick}】"
                f"——{owner}御赐，旁人动不了；本人想改，随口一句话的事。")
        return

    old = runtime.RELATIONS.get(group_id, sender_openid).get("nick")
    runtime.RELATIONS.set_nick(group_id, sender_openid, nick)
    runtime.RENAMES.note(group_id, old, sender_openid)
    runtime.STATE.mark_dirty()
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
    if runtime.OWNER_OPENID:
        rec = runtime.RELATIONS.get(group_id, runtime.OWNER_OPENID, create=False) or {}
        nick = rec.get("nick")
        if nick:
            terms.add(nick)
    return terms


def mentions_owner(text, group_id, mentioned_ids=()):
    """这条消息是不是在说群主：直接 @ 了他，或者提到了他的称呼 / 「群主」二字。"""
    if runtime.OWNER_OPENID and runtime.OWNER_OPENID in tuple(mentioned_ids or ()):
        return True
    return any(t and t in (text or "") for t in owner_reference_terms(group_id))


def banter_chance(base, rec):
    """这条消息触发插嘴的实际概率：基数 × 亲密度权重，再压天花板。

    基数（config.BANTER_PROBABILITY / OWNER_MENTION_PROBABILITY）不动 —— 群里整体
    话量由它决定；权重只回答「更愿意接谁」（relations.BANTER_WEIGHTS）。
    没档案的人权重 1.0：新人不能因为还不熟就被晾着。
    """
    return min(base * relations.banter_weight(rec), config.BANTER_CHANCE_MAX)


def mode_switch_allowed(group_id, openid, is_owner=False):
    """这个人够不够格点单换模式（好感度门槛，2026-09-21 群主提）。

    事故：好感度 −6（一直骂人）的人一句「猫娘模式」就切换成功 —— 模式是群层面的
    公共状态，交给一个跟它还没处熟的人按按钮不合理。

    三条豁免：
      · 群主永远能切（他要调试/演示）；
      · 关掉好感度系统时不拦；
      · **没打过交道的人放行** —— 门槛是防「处得不好还来使唤」，不是防新人。
    """
    if is_owner or not config.AFFINITY_ENABLED:
        return True
    rec = runtime.RELATIONS.get(group_id, openid, create=False)
    if not rec or not rec.get("interactions"):
        return True
    return rec.get("score", 0) >= config.MODE_SWITCH_MIN_AFFINITY


def mode_denied_line(group_id):
    """被拦下时说的话：只给「还没熟到那份上」的感觉，不许报数字、不许提「好感度」。

    写死一句会被念烦，所以几轮换着来；语气带点玩笑，别让人觉得被系统惩罚了。
    """
    return naming.render(random.choice(MODE_DENIED_LINES), owner=owner_label(group_id))


def _reset_style(group_id, old_mode, new_mode):
    """切换模式后把该群的会话历史清掉。

    这是「换了模式只带一点风味，两句话又回去了」的正解：system prompt 每轮都在写，
    但窗口里还压着上一个状态的回复，模型会照着最近几轮自己的语气走。
    清掉历史，新状态的风格第一句就是纯的（关系档案与长期记忆不受影响，那是另一条线）。
    """
    dropped = runtime.SESSIONS.clear_group(group_id)
    runtime.STATE.mark_dirty()
    logger.info("🎭 模式 %s → %s，已清空该群 %d 个会话历史", old_mode, new_mode, dropped)




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
        rec = runtime.RELATIONS.get(group_id, member_openid, create=False)
        nick = (rec or {}).get("nick")
        if nick:
            return nick
        return runtime.DISPLAY_NAMES.of(group_id, member_openid)

    return _fn


def render_digest_reply(group_id, mode="normal"):
    """「群史记」命令：优先读当天压缩出来的 MD（信息最全），没有摘要时再退回简版。

    发出去之前过一遍 refresh_names：「群史记」是长期记忆的原文，里面固化的还是当年的
    称呼，直接发到群里等于当众用旧名叫人。
    """
    md = runtime.ARCHIVE.read_memory(group_id)
    if md:
        body = md.strip()
        if len(body) > 900:
            body = body[:900] + "\n……（太长截了，完整版在 memory/ 目录里，原文在 archive/）"
        return refresh_names(group_id, body)
    return refresh_names(group_id, digest_mod.render_summary(runtime.DIGESTS.get(group_id), mode=mode))


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
    lock = runtime._digest_locks.setdefault(group_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        entries = runtime.ARCHIVE.since(group_id, runtime.DIGESTS.last_run(group_id),
                                limit=config.DIGEST_MAX_PENDING)
        if not entries:
            return
        logger.info("🗜️  开始压缩群 %s 的长期记忆（%d 条待处理 / %s）",
                    group_id[-6:], len(entries), reason or "手动")
        started = time.time()
        try:
            result = await digest_mod.compress_group(
                runtime.DIGESTS, group_id, entries, ask_digest,
                resolver=_resolve_name(group_id),
                budget=runtime.BUDGET.try_consume,
                log=logger.info,
                refresh=lambda t: refresh_names(group_id, t),
                ledger=_affinity_ledger(group_id),
            )
        except Exception as e:
            logger.error("❌ 群记忆压缩异常: %s", str(e)[:200])
            return
        if not result:
            logger.warning("⚠️ 群 %s 记忆未产出结果，原文保留在归档里，下轮再试", group_id[-6:])
            return

        runtime.DIGESTS.mark_compressed(group_id)
        runtime.STATE.mark_dirty()
        logger.info("📜 群 %s 记忆已更新（%.1fs / %d 条）：%s",
                    group_id[-6:], time.time() - started, len(entries),
                    (result.get("brief") or "")[:70])

        # 承诺账本跟着摘要对齐：新出现的记上，摘要里消失的自动撤下
        if config.PROMISE_ENABLED:
            added, dropped = runtime.PROMISES.sync(group_id, (result.get("data") or {}).get("promises"))
            if added or dropped:
                runtime.STATE.mark_dirty()
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
                path = runtime.ARCHIVE.write_memory(
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
            for gid in list(runtime.DIGESTS.groups.keys()):
                pending = len(runtime.ARCHIVE.since(gid, runtime.DIGESTS.last_run(gid)))
                if not runtime.DIGESTS.needs_compress(
                        gid, config.DIGEST_INTERVAL_HOURS, config.DIGEST_MIN_PENDING,
                        pending, count_trigger=config.DIGEST_COUNT_TRIGGER):
                    continue
                await run_digest(gid, reason=runtime.DIGESTS.compress_reason(
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
    dunnable = runtime.PROMISES.dunnable(group_id)
    if not dunnable:
        return
    row = dunnable[0]
    if not runtime.BUDGET.try_consume():
        logger.warning("🛑 今日额度已用尽，本次催债跳过（%d/%d）", runtime.BUDGET.used, runtime.BUDGET.limit)
        return
    runtime.STATE.mark_dirty()
    lines = []
    for r in runtime.PROMISES.list(group_id):
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
    nag = runtime.PROMISES.mark_nagged(group_id, row["id"])
    runtime.STATE.mark_dirty()
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
            for gid in list(runtime.PROMISES.items.keys()):
                # 群里今天都没人说话，就没必要自顾自地开口
                seen = (runtime.STATE.data.get("groups") or {}).get(gid, {}).get("last_seen", 0)
                if time.time() - seen > 86400:
                    continue
                await dun_promises(gid)
        except Exception as e:
            logger.error("⚠️ 催债调度异常: %s", str(e)[:200])












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
        if runtime.OWNER_OPENID:
            logger.info("👑 群主 OpenID 已锁定: %s（好感度锁定顶格；称呼认领后自动生效）", runtime.OWNER_OPENID)
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
                    config.PROVIDER["label"], CANDIDATE_MODELS[0], runtime.BUDGET.used, runtime.BUDGET.limit)
        logger.info("💾 状态=%s | 会话 %s | 关系档案 %s",
                    config.STATE_FILE, runtime.SESSIONS.stats(), runtime.RELATIONS.counts())
        arch = runtime.ARCHIVE.stats()
        logger.info("🗄️  原文归档=%s/ （%d 天 / %d 条）| 摘要 MD=%s/",
                    config.ARCHIVE_DIR, arch["days"], arch["lines"], config.MEMORY_DIR)
        if config.DIGEST_ENABLED:
            pending = {g: len(runtime.ARCHIVE.since(g, runtime.DIGESTS.last_run(g))) for g in runtime.DIGESTS.groups}
            logger.info("🧠 群聊长期记忆已启用 | 攒够 %d 条或每 %.0f 小时压缩一次 | 待压缩 %s",
                        config.DIGEST_COUNT_TRIGGER, config.DIGEST_INTERVAL_HOURS,
                        "、".join(f"群{k[-6:]}:{v}条" for k, v in pending.items()) or "无")
        else:
            logger.info("🧠 群聊长期记忆已关闭（DIGEST_ENABLED=false）")
        if config.PROMISE_ENABLED:
            total = sum(len(v) for v in runtime.PROMISES.items.values())
            logger.info("🧾 承诺催债已启用 | 在账 %d 条 | 宽限 %.0f 小时，最多催 %d 次",
                        total, config.PROMISE_GRACE_HOURS, config.PROMISE_MAX_NAG)
        logger.info("=" * 50)
        asyncio.create_task(probe_provider())

    async def on_group_message_create(self, message: GroupMessage):
        await self.handle_group_msg(message, is_at=False)

    async def on_group_at_message_create(self, message: GroupMessage):
        await self.handle_group_msg(message, is_at=True)

    async def handle_group_msg(self, message: GroupMessage, is_at: bool = False):
        

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
                runtime.STATE.mark_dirty()

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
        # ⚠️ 本地控制指令（「猫娘模式」这类）照样落盘留痕，但打上 cmd 标记 ——
        #    压缩长期记忆时会跳过它们。它们是操作不是聊天，进了压缩就变成
        #    「这人想变猫娘」这种人物事实（2026-09-21 实测事故）。
        runtime.ARCHIVE.append(group_id, sender_openid, user_input, at=is_at,
                       cmd=is_control_command(user_input))
        if config.DIGEST_ENABLED:
            runtime.DIGESTS.touch(group_id)
        # 记一下这个群最近有人说话：主动催债前要确认群里还活着。
        # 就地更新而不是整块替换 —— 这个槽位还存着别的每群标记（比如群主认领提示有没有说过）。
        runtime.STATE.data.setdefault("groups", {}).setdefault(group_id, {})["last_seen"] = time.time()
        runtime.STATE.mark_dirty()

        # 3.5 写入群聊滑动窗口背景缓存。
        #     只挡「同一个人连着刷一模一样的内容」（复读机、表情三连），别让它把窗口占满；
        #     其余照收。以前这里是「? / 6 / 草 / 哈哈」之类的白名单枚举，来一句新的
        #     口头禅就得回来加一个词，而且把「？」这种真实的接话信号也一起挡掉了。
        buf = group_buffers[group_id]
        if not buf or buf[-1]["sender"] != sender_openid or buf[-1]["text"] != user_input:
            buf.append({"sender": sender_openid, "text": user_input, "time": time.time()})
            runtime.STATE.data["buffers"][group_id] = list(buf)

        # 4. 群主认领 —— **群里永远不授权**，这里只负责把人指到私聊去。
        #    以前这段真的会在群里认主（谁先喊谁得），等于把身份挂在大喇叭上招领；
        #    现在群里说破天也只是收到一句「去私聊办」，权限一个字都不给。
        if looks_like_claim_attempt(user_input):
            owner = owner_label(group_id)
            if runtime.OWNER_OPENID is None:
                # 每个群只正面回一次，免得有人反复喊就反复刷屏
                if claim_redirect_pending(group_id):
                    await safe_reply(message, OWNER_CLAIM_HINT)
                return
            if runtime.OWNER_OPENID == sender_openid:
                await safe_reply(message, f"{owner}，您早就认领过了 —— 您的 OpenID 已经焊死在我的"
                                          "系统核心里，有什么吩咐直接说就行。")
                return
            await safe_reply(message,
                f"（战术后仰并投来极度嫌弃的目光）\n差不多得了！真正的{owner}"
                "早就私聊跟我对完暗号了，你个山寨货还想在这儿篡位夺权呢？"
                f"信不信我现在就向{owner}打小报告，给你安排个禁言大礼包？")
            return

        is_owner = runtime.OWNER_OPENID is not None and sender_openid == runtime.OWNER_OPENID

        # 群模式（顺便把旧的 cadre 存档迁移成 discipline）
        current_mode = resolve_mode(runtime.STATE.get_mode(group_id))
        if current_mode != runtime.STATE.get_mode(group_id):
            runtime.STATE.set_mode(group_id, current_mode)

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
        if is_owner and hits(user_input, OWNER_COMMANDS):
            await safe_reply(message,
                "（唰地拔出四十米纯钛合金绣春刀，单膝跪地抱拳）\n"
                f"锦衣卫{naming.bot_name()}领旨！大胆刁民，竟敢触犯天颜！\n"
                f"本卫已将该狂徒打入《群聊诛九族大牢》，剥夺摸鱼政治权利终身！{owner_label(group_id)}，"
                "您看是直接拖去午门斩首，还是没收其键盘三年？小的立刻去办！")
            return

        # 6. 极速本地彩蛋：赛博决斗（免@ 0 延迟，结果也计入交情）
        if hits(user_input, DICE_TRIGGERS):
            await reply_duel(message, group_id, sender_openid)
            return

        # 7. 模式切换（免@生效，纯本地文案，不消耗 AI）
        if hits(user_input, exit_triggers()):
            if current_mode != "normal":
                runtime.STATE.set_mode(group_id, "normal")
                _reset_style(group_id, current_mode, "normal")
                line = MODE_EXIT_LINES.get(current_mode, "收到！已恢复普通{bot}模式！")
                await safe_reply(message, naming.render(line, owner=owner_label(group_id)))
                return

        for mode_name, triggers, entry_line in MODE_ENTRIES:
            if current_mode != mode_name and hits(user_input, triggers):
                # 交情不够别来点单：模式是整群的公共状态，不能让一个跟它还没处熟的人按按钮
                if not mode_switch_allowed(group_id, sender_openid, is_owner):
                    logger.info("🚫 模式切换被交情门槛拦下（%s → %s）",
                                mode_name, sender_openid[-4:])
                    await safe_reply(message, mode_denied_line(group_id))
                    return
                runtime.STATE.set_mode(group_id, mode_name)
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
                        runtime.STATE.data["cooldowns"][group_id] = now
                        runtime.STATE.mark_dirty()
                        await safe_reply(message, meme_value)
                        return

        # 8.5 关系档案本地查询（直接读档，0 token，不打扰模型）
        if matches_relation_query(user_input):
            rec = runtime.RELATIONS.get(group_id, sender_openid)
            await safe_reply(message, relations.render_status(rec, sender_openid, mode=current_mode))
            return

        if hits(user_input, RELATION_BOARD_TRIGGERS):
            rows = runtime.RELATIONS.rank(group_id, topn=config.AFFINITY_BOARD_SIZE)
            await safe_reply(message, relations.render_rank(rows, mode=current_mode))
            return

        # 名字绑错人时用它当场核对：谁的名字挂在哪个人头上
        if hits(user_input, NAME_TABLE_TRIGGERS):
            rows = _all_named(group_id)
            await safe_reply(message, relations.render_name_table(rows, mode=current_mode))
            return

        if config.DIGEST_ENABLED and hits(user_input, DIGEST_TRIGGERS):
            await safe_reply(message, render_digest_reply(group_id, current_mode))
            return

        # 8.7 承诺台账：查账是本地读取，结清按昵称匹配撤下
        if config.PROMISE_ENABLED:
            if hits(user_input, PROMISE_CLEAR_TRIGGERS):
                rec = runtime.RELATIONS.get(group_id, sender_openid)
                nick = (rec or {}).get("nick") or ""
                removed = runtime.PROMISES.resolve(group_id, keyword=nick or sender_openid[-4:])
                if removed:
                    runtime.STATE.mark_dirty()
                    await safe_reply(message,
                        f"（在小本本上重重划掉 {removed} 行）行，算你说话算话，这笔账销了。")
                else:
                    await safe_reply(message, "（翻遍台账）你名下好像没欠着什么啊，别急着邀功。")
                return

            if hits(user_input, PROMISE_TRIGGERS):
                # 同上：发到群里之前先刷新，别拿旧名当众叫人
                await safe_reply(message, refresh_names(group_id, runtime.PROMISES.render(group_id)))
                return

        # 8.8 回原文查证：摘要出错时用它翻底稿
        m_lookup = RE_LOOKUP.match(user_input)
        if m_lookup:
            found = runtime.ARCHIVE.search(group_id, m_lookup.group(1), limit=8)
            if not found:
                await safe_reply(message, f"（翻遍归档）没找着含「{m_lookup.group(1)}」的发言，"
                                          "要么你记错了，要么这事只在你脑子里发生过。")
            else:
                lines = []
                for h in found[-8:]:
                    when = time.strftime("%m-%d %H:%M", time.localtime(h["ts"]))
                    # 翻旧账是**发出去给人看的**：称呼 > 显示名 > 泛称，不吐 openid
                    who = (_resolve_name(group_id)(h["sender"])
                           or runtime.DISPLAY_NAMES.of(group_id, h["sender"])
                           or "某位群友")
                    lines.append(f"[{when}] {who}：{h['text'][:60]}")
                await safe_reply(message,
                    f"🔍 【翻旧账·{m_lookup.group(1)}】共 {len(found)} 条，最近这些：\n" + "\n".join(lines))
            return

        # 8.9 手动压一次：不用等定时器，方便立刻验证效果
        if config.DIGEST_ENABLED and hits(user_input, MANUAL_DIGEST_TRIGGERS):
            await safe_reply(message, "（摊开小本本，把最近的聊天从头捋一遍）稍等，我理一理。")
            await run_digest(group_id, reason="手动触发")
            await safe_reply(message, render_digest_reply(group_id, current_mode))
            return

        # 9. 判定这条要不要接
        is_fortune = hits(user_input, FORTUNE_TRIGGERS)

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
                # 亲密度权重：越熟的人说话越容易被接（2026-09-21 群主提的机制）。
                # 权重不是概率 —— 基数不动，熟人 1.6×、生人 0.7×，整体话量不变。
                rec = runtime.RELATIONS.get(group_id, sender_openid, create=False)
                chance = banter_chance(chance, rec)
                if random.random() < chance:
                    should_reply = True
                    is_random = True
                    logger.info("🎲 插嘴命中（亲密度权重 %.2f → 概率 %.3f，%s）",
                                relations.banter_weight(rec), chance, sender_openid[-4:])
                    group_last_random_reply[group_id] = now
                    runtime.STATE.data["cooldowns"][group_id] = now
                    runtime.STATE.mark_dirty()

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
            # ⚠️ 必须按时间过滤（2026-10-06 实测）：buffer 是落盘的、跨重启保留，
            # 机器睡了 12 天再醒来的第一条消息，配到的还是 12 天前的对话 ——
            # 那不叫「最近背景」，叫考古。
            now_ts = time.time()
            fresh = [b for b in buf if now_ts - float(b.get("time") or 0)
                     <= config.GROUP_BUFFER_TTL_SECONDS]
            prev = [b for b in fresh[:-1] if b["text"] != user_input][-3:]
            if prev:
                # 必须标出发言人。之前只给一串裸文本，模型分不清哪句是谁说的，
                # 于是把别人认领的名字安到了当前这位头上 —— 叫错人就是这么来的
                def _who(oid):
                    # 认领过的称呼 > 群里挂的显示名 > 泛称。以前这里拼 openid 后四位，
                    # 结果模型把它当成名字复读进了回复 —— 群里没人看得懂。
                    r = runtime.RELATIONS.get(group_id, oid, create=False)
                    return ((r or {}).get("nick")
                            or runtime.DISPLAY_NAMES.of(group_id, oid)
                            or "一位群友")

                context_hint = refresh_names(
                    group_id, "\n".join(f"- {_who(b['sender'])}：{b['text']}" for b in prev))
                # ⚠️ 这行是对着「爱翻旧账的模型」写的（实测 Gemini 会把背景里别人的言行安到
                # 当前说话人头上，一晚上把「当猫娘」安给了三个人）。所以要点名三件事：
                # 每句都有主、主语不是眼前这位、引用前先核对名字。
                context_hint = ("（注意：下面每一句开头都标了发言人 —— 这些话是「他们」说的，"
                                "不是眼前这位说的。别把别人干的蠢事算到当前说话人头上，"
                                "引用谁的言行就对准谁的名字，拿不准是谁就别点名；"
                                "也别刻意复读这些内容）\n" + context_hint)

        active_mode = current_mode
        if is_fortune and current_mode == "normal":
            active_mode = "fortune"

        # 每人每群一份独立 session，彻底隔离会话，绝不串台
        session_id = f"{group_id}_{sender_openid}"

        # 关系档案注入：告诉模型「对面这个人跟你有过多深的往来」，同时消费掉待播报的里程碑
        relation_note = ""
        target_record = None
        if config.AFFINITY_ENABLED:
            target_record = runtime.RELATIONS.get(group_id, sender_openid)
            relation_note = relations.build_relation_note(
                target_record, sender_openid, mode=active_mode,
                other_names=_other_names(group_id, sender_openid),
            )
            if target_record.get("pending_milestone"):
                # 消费掉，并记下「今天已经报过一次」—— 台阶播报一天只说一次，
                # 免得放宽日上限之后它把同一句播成每日常态（见 relations.apply 里的说明）。
                target_record["pending_milestone"] = None
                target_record["told_day"] = time.strftime("%Y-%m-%d")
                runtime.STATE.mark_dirty()

        # 群聊长期记忆：有就注入，让它可以说「上周撺掇打牌那事儿我可还记着」
        group_memory = ""
        if config.DIGEST_ENABLED:
            summary = runtime.DIGESTS.get(group_id)
            if summary and (summary.get("brief") or (summary.get("data") or {}).get("topics")):
                # 名字统一刷新成当前称呼再注入：模型永远拿不到旧名，也就编不出
                # 「那谁和这谁」这种把同一个人拆成两个人的往事。
                # ⚠️ 人物名册（render_people）必须跟着一起注入：2026-09-20 实测，光给
                # 【群史记】的连动长句，模型读的时候会把主语拧错（把「家豪解封」说成
                # 「老王解封」）；「谁：什么事」一行一人的名册才是它能对准的事实底账。
                people_block = digest_mod.render_people(summary)
                if people_block:
                    people_block = "\n" + people_block
                # 承诺台账一天只在上下文里露一次（PromiseBook.take_for_prompt）：
                # 「没兑现的某某事」每轮都在眼前，就会被当成万能收尾句用。
                # 这里只改喂给模型的那份视图，state 里的摘要原文不动。
                view = summary
                promises = (summary.get("data") or {}).get("promises")
                if promises:
                    visible = runtime.PROMISES.take_for_prompt(group_id, promises)
                    if len(visible) != len(promises):
                        data = dict(summary.get("data") or {})
                        data["promises"] = visible
                        view = dict(summary, data=data)
                        runtime.STATE.mark_dirty()
                group_memory = refresh_names(
                    group_id,
                    digest_mod.render_summary(view, mode=active_mode)
                    + people_block)
                # 引导语见 prompts.PROMPT_MEMORY_HEADER：除了说清「是真的」，
                # 还得说清「是旧事、别当万能梗」—— 否则它会逮着一件事反复说。
                group_memory = prompts.PROMPT_MEMORY_HEADER + "\n" + group_memory

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

        # 日内微调（2026-10-06 改）：
        #   「被 @ 且没扣分就 +1」撤掉了 —— 那等于把好感度变成「被搭理次数」，
        #   实测一天 100 多次记账里 108 次 +1，连一直骂人的也在涨。
        # 现在是：模型判**方向**（-1/0/1，见 CMD_PROTOCOL），拿不到模型判定时退回本地词表，
        #   最后统一过每日封顶。真正的印象结算仍在压缩里做（它看得到一整天，更准）。
        if config.AFFINITY_ENABLED:
            if delta is None:
                delta = relations.local_sentiment(user_input)
                apply_affinity_delta_capped(group_id, sender_openid, delta, "词表兜底")
            else:
                # reason 区分开是有用的：跑几天就能从日志里看出「模型判方向」和
                # 「词表兜底」各自的占比与准头，再决定要不要把权重挪向压缩结算。
                apply_affinity_delta_capped(group_id, sender_openid, delta, "模型判方向")

    async def on_c2c_message_create(self, message: Message):
        """私聊。以前这里连去重都没有，重复投递会重复扣额度。"""
        
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
            if runtime.OWNER_OPENID is None:
                runtime.OWNER_OPENID = sender_openid
                save_owner(runtime.OWNER_OPENID)
                runtime.RELATIONS.pin(runtime.OWNER_OPENID)   # 好感度钉在顶格：亲近靠档案，不靠谄媚台词
                runtime.STATE.mark_dirty()
                logger.info("👑 【认主成功】已永久锁定唯一群主 OpenID: %s（好感度锁定顶格）",
                            runtime.OWNER_OPENID)
                await safe_reply(message,
                    "（当场立正敬礼，掏出纯金VIP打卡机录入指纹）\n滴！认主成功！"
                    f"从今往后{naming.bot_name()}唯您马首是瞻 ——"
                    "给别人起名、特权指令这些，往后只有您说了算。")
                return
            if runtime.OWNER_OPENID == sender_openid:
                await safe_reply(message,
                    "您早就认领过了 —— 您的 OpenID 已经焊死在我的系统核心里，不用再对一次。")
                return
            # 已经有主了。**不透露是谁**：只报「有人认领过」，一个字都不多说。
            await safe_reply(message, "（礼貌地鞠了个躬）这边已经有人认领过我了，就不另立山头了。")
            return

        # 说了句「像是要认领」的话但没对上暗号。**静默失效正是这个功能最大的坑**，所以明说，
        # 而且这句是本地固定话术：0 token，也不占每日额度。
        if runtime.OWNER_OPENID is None and looks_like_claim_attempt(user_input):
            if config.OWNER_CLAIM_PHRASE:
                await safe_reply(message,
                    "这是想认领我？那得说对暗号才行 —— 暗号是部署的时候在 `OWNER_CLAIM_PHRASE` "
                    "里配的那一句。")
            else:
                await safe_reply(message,
                    "想认领我？这台机器的私聊认主通道是关着的 —— 部署的人没有配 "
                    "`OWNER_CLAIM_PHRASE`。")
            return

        is_owner = runtime.OWNER_OPENID is not None and sender_openid == runtime.OWNER_OPENID

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

        if not runtime.BUDGET.try_consume():
            logger.warning("🛑 今日额度已用尽，私聊也一并拒绝（%d/%d）", runtime.BUDGET.used, runtime.BUDGET.limit)
            runtime.STATE.mark_dirty()
            await safe_reply(message, "（小声）今天额度用冒了，明天再陪你聊啊兄弟。")
            return
        runtime.STATE.mark_dirty()

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
            runtime.STATE.save_now(force=True)
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
            dropped = runtime.SESSIONS.prune()
            if dropped:
                runtime.STATE.mark_dirty()
                logger.info("🧹 已清理 %d 个过期会话，当前 %s", dropped, runtime.SESSIONS.stats())
            stale = runtime.RELATIONS.prune()
            if stale:
                runtime.STATE.mark_dirty()
                logger.info("🧹 已清理 %d 份长期失联的关系档案，当前 %s", stale, runtime.RELATIONS.counts())
        except Exception as e:
            logger.error("⚠️ 会话清理异常: %s", e)


def run_bot():
    errors = config.validate()
    if errors:
        raise SystemExit("❌ 配置缺失：\n  - " + "\n  - ".join(errors))

    setup_logging()
    patch_aiohttp_ssl()
    install_ws_watchdog()
    load_owner()

    _main_models = MODEL_CHAINS.get(config.AI_PROVIDER) or []
    logger.info("🤖 供应商=%s | 主模型=%s | 梯队=%s",
                config.PROVIDER["label"],
                _main_models[0] if _main_models else "（无可用 Key）",
                " → ".join(config.PROVIDER_PRESETS[n]["label"] for n in PROVIDER_CHAIN))
    if runtime.SESSIONS._sessions:
        logger.info("💾 已恢复 %s 个会话上下文", len(runtime.SESSIONS._sessions))

    install_signal_handlers()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(runtime.STATE.flush_loop())
    loop.create_task(ws_watchdog_loop())
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
                runtime.STATE.save_now(force=True)
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
            runtime.STATE.save_now(force=True)
        except Exception:
            pass
