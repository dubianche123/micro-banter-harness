"""昵称域工具：@ 学习、改名三链同步、改名节流与起名锁（2026-10-06 从 bot.py 拆出）。

只放**工具与判定**；两个 async 指令处理器（handle_nick_lock / handle_nick_command）
留在 bot.py —— 它们是「指令路由」，依赖 safe_reply / judge_nick 这类回复层。
"""
import re
import time

import config
import naming
import qqtext
import relations
import logging
logger = logging.getLogger("qqbot")   # 与 bot.py 同一个命名 logger，共享 handler

import runtime

from runtime import owner_label, hits, _all_named

# ⚠️ 句点**不在**断点里：「A.A」「L.L」这种是真名号，切了就只剩一个字、
# 还会被「单字不记」挡掉。代价是「@老王.你好」会连着吃进去，中文群里极少见。
_RE_PLAIN_MENTION = re.compile(r"@([^\s@，。！？；：、,!?;:（）()【】\[\]]{1,24})")
_RE_TRAILING_DOTS = re.compile(r"[.．]+\Z")
# 裸的机器编号（openid 那类），没有尖括号裹着时靠它兜底。没有人的昵称长这样。
_RE_MACHINE_ID = relations._RE_MACHINE_ID   # 同一条规则只允许有一份（relations 是机器 ID 判定的家）

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


def mention_nicks_from_content(content, bot_names=()):
    """按正文里出现的顺序，抠出所有 @ 后面的**明文**昵称。

    返回顺序和 `mentions` 一致，所以调用方只要校验**数量对得上**就能逐个配对。
    机器人自己的叫法要跳过（@它就是喊它，不是某个人）—— 这一步同时把 mentions
    里机器人那一格对齐剔掉，否则后面的配对会整体错位。

    ⚠️ 必须先剔掉机器形态的 `<@!openid>` / `<@openid>`：它是 openid 不是昵称，
    但不剔就会被下面的正则当成明文吞进去，还被 24 字上限切成一段残缺编号 ——
    实测就这么把一串 openid 前缀学成了「某人叫 一长串机器编号」，
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
    """拿一条消息里的明文 @昵称 去喂 `runtime.DISPLAY_NAMES`，返回学到了几个。

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
        if runtime.DISPLAY_NAMES.learn(group_id, oid, nick):
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
    owner = runtime.OWNER_OPENID

    def resolve(openid):
        if bot_id and openid == bot_id:
            return bot_display or naming.bot_name()
        if owner and openid == owner:
            return owner_label(group_id)
        rec = runtime.RELATIONS.get(group_id, openid, create=False) or {}
        return rec.get("nick") or runtime.DISPLAY_NAMES.of(group_id, openid)

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
    旧名从哪来：runtime.RENAMES（改名/撤销时登记的台账）。它自己永远不进上下文。

    current_names 传的是本群**主名**名单：称谓分主副，主名是当事人自己定的那一版，
    台账只负责把副名（旧叫法）改写成主名，反过来绝不许动主名。
    """
    if not text:
        return text
    return runtime.RENAMES.refresh(
        group_id, text,
        label_of=lambda oid: (runtime.RELATIONS.get(group_id, oid, create=False) or {}).get("nick"),
        current_names=runtime.RELATIONS.main_names(group_id),
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
    if old in runtime.RELATIONS.main_names(group_id):
        logger.info("⏭️ 跳过改名同步：「%s」同时还是别人的主名", old)
        return False
    n1 = runtime.SESSIONS.rename_user(group_id, old, new)
    n2 = runtime.DIGESTS.rename_in_memory(group_id, old, new)
    n3 = runtime.ARCHIVE.rename_in_memory_files(group_id, old, new)
    if n1 or n2 or n3:
        runtime.STATE.mark_dirty()
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
    if old in runtime.RELATIONS.main_names(group_id):
        logger.info("⏭️ 跳过撤销同步：「%s」同时还是别人的主名", old)
        return False
    label = relations.UNNAMED_LABEL
    n1 = runtime.SESSIONS.rename_user(group_id, old, label)
    n2 = runtime.DIGESTS.rename_in_memory(group_id, old, label)
    n3 = runtime.ARCHIVE.rename_in_memory_files(group_id, old, label)
    if n1 or n2 or n3:
        runtime.STATE.mark_dirty()
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
    if runtime.OWNER_OPENID and subject_openid != runtime.OWNER_OPENID:
        names.add(owner_label(group_id))
    return names


def nick_locked(group_id):
    """这个群的起名锁当前是不是锁着的。状态存在每群自己的槽位里，重启不丢。"""
    return bool((runtime.STATE.data.get("groups") or {}).get(group_id, {}).get("nick_locked"))


def set_nick_locked(group_id, locked):
    """掰起名锁。⚠️ 就地更新槽位 —— 整块替换会把同槽的其他标记冲掉。"""
    runtime.STATE.data.setdefault("groups", {}).setdefault(group_id, {})["nick_locked"] = bool(locked)
    runtime.STATE.mark_dirty()


def _taken_nick_names(group_id, subject_openid=""):
    """**已被别人占用**的主名 —— 主名不能重叠，这份就是占用名单。

    跟 `_reserved_nick_names` 分开，只为了把话说清楚：「冒名顶替」和「重名」是两种不同的
    拒绝理由，一个说「不让用」，一个说「已经有人叫这个了」。群主本来就有改名权，
    他撞上的是后者，提示得对得上他才不会以为功能坏了。

    按 openid 排掉「这一笔写给的那个人」自己那一版 —— 本人随时可以覆盖自己的名字，
    这条不能挡，否则「改名随时能改」就成了空话。
    """
    return runtime.RELATIONS.main_names(group_id, exclude_openid=subject_openid)


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
