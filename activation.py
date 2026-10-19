"""激活度：作息、心情、亲密度融成一锅——一次回复的全部「内在状态」。

设计原则（群主 2026-10-19 定调）：三个信号融合成**一个**激活度轮廓，
不各自成系统、除心情的落盘外不碰模型、全部纯计算。

- 作息/生物钟：消息自带时间戳。凌晨的消息是「把人吵醒」——插嘴意愿打折，
  真回了也带着起床气；这不是一个定时器，是看消息几点发的。
- 心情：小王自己的情绪，全局一个值（-1 烦 → +1 顺）。事件驱动：每轮好感度
  结算时喂一笔（模型判的方向现成的，零额外调用）；读取时按半衰期自然回中。
  落 state.json（mood 键），重启不丢。
- 关注度：亲密度决定能看到的背景条数——越熟的人，它记得越多。

对外只有一个入口：profile(tier, hour) → {willingness, turns, state_line}。
"""

import time
from datetime import datetime

# ── 作息/生物钟 ──
# (起始小时, 插嘴意愿系数, 状态词)。状态词为空 = 不到注入提示词的程度。
# 系数是**乘数**：乘在插嘴基数 × 亲密度权重之后。深夜不是不回，是不太会主动搭话；
# 被 @ 是另一回事——醒着的人被点名也得应，但语气是起床气。
_CIRCADIAN = (
    (0,  0.45, "凌晨被吵醒"),
    (6,  0.75, "刚睡醒"),
    (10, 1.00, ""),
    (13, 0.90, "午后有点困"),
    (15, 1.00, ""),
    (23, 0.70, "熬到半夜"),
)


def circadian(hour=None):
    """按小时返回 (插嘴意愿系数, 状态词)。hour 不传就用现在。"""
    hour = datetime.now().hour if hour is None else hour
    factor, label = 1.0, ""
    for start, f, label_ in _CIRCADIAN:
        if hour >= start:
            factor, label = f, label_
    return factor, label


# ── 心情 ──
_MOOD_HALFLIFE = 6 * 3600.0     # 6 小时半衰期：早上被惹的烦，晚上该消得差不多了
_MOOD_MAX = 1.0
_mood_state = {"v": 0.0, "ts": 0.0}


def hydrate_mood(raw):
    if isinstance(raw, dict):
        try:
            _mood_state["v"] = max(-_MOOD_MAX, min(_MOOD_MAX, float(raw.get("v") or 0.0)))
            _mood_state["ts"] = float(raw.get("ts") or 0.0)
        except (TypeError, ValueError):
            pass


def dump_mood_into(data):
    if _mood_state["ts"]:
        data["mood"] = {"v": round(_mood_state["v"], 3), "ts": _mood_state["ts"]}


def _decay(now):
    dt = max(0.0, now - _mood_state["ts"])
    return _mood_state["v"] * (0.5 ** (dt / _MOOD_HALFLIFE))


def note_mood(delta, now=None):
    """每轮好感度结算后喂一笔（delta 是裁剪过的方向值，正=被善待）。"""
    now = time.time() if now is None else now
    v = max(-_MOOD_MAX, min(_MOOD_MAX, _decay(now) + delta * 0.25))
    _mood_state["v"] = v
    _mood_state["ts"] = now


def current_mood(now=None):
    """当前有效心情（已按半衰期回中），[-1, 1]。纯读——不许刷新衰减时钟。"""
    now = time.time() if now is None else now
    return max(-_MOOD_MAX, min(_MOOD_MAX, _decay(now)))


def _mood_word(v):
    if v >= 0.3:
        return "心情不错"
    if v <= -0.3:
        return "有点烦"
    return ""


def mood_dice_factor(v):
    """心情对插嘴意愿的乘数：心情好更愿意搭话，烦了就更懒得理（±15% 封顶）。"""
    return 1.0 + 0.15 * max(-1.0, min(1.0, v))


# ── 关注度：亲密度档位 → 背景条数 ──
# 越熟记得越多。生人不是不记，是没必要让它看到太多——它跟人家还没熟到翻旧账的程度。
_CONTEXT_TURNS = {
    "soulmate": 10, "buddy": 8, "friend": 6,
    "acquaintance": 4, "familiar": 3, "distant": 2,
}


def context_turns(tier):
    return _CONTEXT_TURNS.get(tier, 3)


# ── 总装 ──
def state_line(label, mood_v):
    """把作息 + 心情渲染成一段注入提示词的状态自述。没什么可说时返回空串。"""
    bits = []
    if label == "凌晨被吵醒":
        bits.append("你是刚被这条消息吵醒的：回复要短、带着起床气或不耐烦的困，别展开任何话题")
    elif label == "熬到半夜":
        bits.append("夜深了，你有点撑不住了：回复更短更含糊，别开新话题")
    elif label == "刚睡醒":
        bits.append("你刚醒没多久，脑子还没完全转起来")
    mw = _mood_word(mood_v)
    if mw == "有点烦":
        bits.append("你今天有点烦：更懒得接话，别人起哄你懒得陪")
    elif mw == "心情不错":
        bits.append("你今天心情不错：更愿意接梗闲聊两句")
    if not bits:
        return ""
    return "【小王当前状态】" + "；".join(bits) + "。（这是你的真实体感，顺着它说话，别念出来）"


def profile(tier, hour=None, now=None):
    """一次回复的完整内在状态。

    返回 (willingness, turns, state_line)：
    - willingness  插嘴意愿总乘数（作息 × 心情），乘在基数 × 亲密度权重之后
    - turns        这位亲密度能看到的背景条数
    - state_line   注入提示词的状态自述（没什么可说时为空串）
    """
    now = time.time() if now is None else now
    factor, label = circadian(hour)
    mood_v = current_mood(now)
    willingness = factor * mood_dice_factor(mood_v)
    return willingness, context_turns(tier), state_line(label, mood_v)


# ── 主动性 ──
# 不是定时器：所有闸门过了才开口，节奏由状态决定（麦麦/Agent Mind 同款思路的轻量版）。
_PROACTIVE_MIN_GAP = 4 * 3600.0      # 距上次主动发言至少 4 小时
_PROACTIVE_QUIET_MIN = 10 * 60.0     # 群静默 10 分钟以上（还热着轮不到我插话）
_PROACTIVE_QUIET_MAX = 6 * 3600.0    # 但凉透超过 6 小时也不硬聊（没人接）


def proactive_ok(mood_v, hour, now, last_proactive_ts, last_msg_ts):
    """主动开口的闸门：纯计算。返回 (ok, 原因)。"""
    if now - (last_proactive_ts or 0) < _PROACTIVE_MIN_GAP:
        return False, "距上次主动发言不够 4 小时"
    if hour < 8 or hour >= 24:
        return False, "深夜/清晨不主动开口"
    quiet = now - (last_msg_ts or 0)
    if quiet < _PROACTIVE_QUIET_MIN:
        return False, "群还热着，轮不到我插话"
    if quiet > _PROACTIVE_QUIET_MAX:
        return False, "群凉透了，开口也没人接"
    if mood_v <= -0.3:
        return False, "心情不好，不想开口"
    return True, ""
