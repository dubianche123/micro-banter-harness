"""好感度结算：逐轮记账 + 每日封顶 + 压缩结算落地（2026-10-06 从 bot.py 拆出）。

单例从 runtime 取（分数档案 runtime.RELATIONS、额度账本 runtime.STATE、承诺台账 runtime.PROMISES）。
"""
import time

import config
import relations
import logging
logger = logging.getLogger("qqbot")   # 与 bot.py 同一个命名 logger，共享 handler

import runtime
from runtime import _all_named

def apply_relation_delta(group_id, member_openid, delta, reason=""):
    """把一次互动的情感分值写进档案。

    默认情况下这里是**本地关键词兜底**在打分（见下方调用点）—— 让模型每轮自评那套已经撤了，
    好感度的模型判断挪到每日压缩里做（apply_digest_affinity）。若哪天把
    config.CMD_PROTOCOL_ENABLED 打开回退，这里又会优先用模型给的分值。
    """
    if not config.AFFINITY_ENABLED:
        return None
    score, old_lv, new_lv = runtime.RELATIONS.apply(group_id, member_openid, delta)
    runtime.STATE.mark_dirty()
    logger.info("💗 [%s/%s] %+d → %d 档位=%s%s %s",
                group_id[-6:], member_openid[-6:], delta, score, new_lv,
                f"（原{old_lv}）" if old_lv != new_lv else "", reason)
    return score


def _affinity_daily_key(group_id, member_openid):
    return f"{group_id}|{member_openid}"


def affinity_budget(group_id, member_openid, now=None):
    """今天这个人还剩多少加减分额度。跨天自动重置（只留今天和昨天，别让存档无限长）。"""
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    yesterday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    table = runtime.STATE.data.setdefault("affinity_daily", {})
    for k in [k for k, v in table.items()
              if isinstance(v, dict) and v.get("day") not in (day, yesterday)]:
        table.pop(k, None)
    rec = table.get(_affinity_daily_key(group_id, member_openid)) or {}
    if rec.get("day") != day:
        rec = {"day": day, "gain": 0, "loss": 0}
        table[_affinity_daily_key(group_id, member_openid)] = rec
    return rec


def apply_affinity_delta_capped(group_id, member_openid, delta, reason="", span=None,
                                now=None, count=True):
    """带**每日封顶**的好感度写入。返回真正落库的分数（None = 被封顶吃掉了）。

    为什么非要封顶：好感度是长期账本，而打分有两个会出错的来源 ——
    模型的判断会漂、本地词表会有冤案。没有封顶的话，一次误判就能把「铁哥们」
    直接扣成「生疏」，而涨回来要很多天。封顶把单次事故的伤害压到可恢复的范围内。
    封顶是**不对称**的：加分放宽（关系本来就该越聊越近），扣分收紧（误判更伤人）。
    """
    if not config.AFFINITY_ENABLED or not delta:
        return None
    budget = affinity_budget(group_id, member_openid, now)
    # ⚠️ 这里**不**做幅度 clamp：压缩结算一次能到 ±6，削成 ±3 就把它的语义废了。
    # 幅度的收窄交给 runtime.RELATIONS.apply（它知道 span 是一次互动还是一整天）。
    # 这里只管**封顶截断**，让单日额度真的成为硬上限。
    delta = int(delta)
    # ⚠️ 关键：不只是「超了没」，还得把这一笔**截断到剩余额度** ——
    #    压缩结算一次能到 ±6，只判断不截断的话，一次就能把一天的额度穿个洞（实测 -6 > cap 5）。
    if delta > 0:
        room = config.AFFINITY_DAILY_GAIN_CAP - budget["gain"]
        if room <= 0:
            logger.info("💗 [%s/%s] %+d 被日封顶吃掉（今天已加 %d/%d）",
                        group_id[-6:], member_openid[-6:], delta,
                        budget["gain"], config.AFFINITY_DAILY_GAIN_CAP)
            return None
        delta = min(delta, room)
    elif delta < 0:
        room = config.AFFINITY_DAILY_LOSS_CAP - budget["loss"]
        if room <= 0:
            logger.info("💗 [%s/%s] %d 被日封顶吃掉（今天已扣 %d/%d）",
                        group_id[-6:], member_openid[-6:], abs(delta),
                        budget["loss"], config.AFFINITY_DAILY_LOSS_CAP)
            return None
        delta = -min(-delta, room)
    if not delta:
        return None
    budget["gain"] += max(0, delta)
    budget["loss"] += max(0, -delta)
    runtime.STATE.mark_dirty()
    if span is None and count:
        return apply_relation_delta(group_id, member_openid, delta, reason)
    return runtime.RELATIONS.apply(group_id, member_openid, delta, span=span, count=count)


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
        # 压缩结算也走同一道每日封顶 —— 它一次能到 ±6，不封的话一天就能把分数洗一遍
        got = apply_affinity_delta_capped(
            group_id, target, delta, reason="压缩结算",
            span=config.AFFINITY_DIGEST_SPAN, count=False)
        if got is None:
            notes.append(f"{who}{delta:+d}→封顶")
            continue
        score, old_lv, new_lv = got
        applied += 1
        notes.append(f"{who}{delta:+d}→{score}")
    if applied:
        runtime.STATE.mark_dirty()
    return applied, "、".join(notes)


def _affinity_ledger(group_id):
    """给压缩结算看的「关系底账」：每个人现在站在哪个交情档位。

    玩笑和敌意的分界跟着交情走 —— 熟人损它十句是日常，生人损十句是敌意。
    压缩要判「关系走势」，得先知道每段关系现在站在哪，否则两把尺子量所有人。
    这块只进压缩 prompt（内部结算用），不进群聊回复，档位名可以直接写。
    """
    if not config.AFFINITY_ENABLED:
        return None
    rows = []
    for _oid, rec in _all_named(group_id):
        key = relations.level_key(rec.get("score", 0))
        rows.append(f"{rec.get('nick')}={relations.level_label(key)}")
    if not rows:
        return None
    return ("【当前关系底账】以下是他和各人现在的交情档位，"
            "判断玩笑还是敌意之前先看这个：\n" + "、".join(rows))
