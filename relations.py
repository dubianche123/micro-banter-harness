"""群友关系档案 —— 把「机器人认识谁、跟谁熟」变成代码里的真实状态。

为什么需要这个文件
------------------
改造前，关于群友的一切认知都活在 LLM 的一次性上下文里：聊完就忘，换个 session
或者重启一次进程，昨天还称兄道弟的人今天就成了陌生人。更糟的是模型会「即兴发挥」
—— 你问它跟张三熟不熟，它能编出一堆从来没发生过的往事，而且每次编的还不一样。

这里把关系量化成一个持久化整数，AI 只负责两件事：
  1. 判断这一次互动的情感倾向（顺带输出一个标签，不额外花钱）
  2. 把分数翻译成说话的分寸感
记账、分级、衰减、排名全是本地代码干的，模型碰不到数字，也就没法编。

同一个分数在不同模式下会被演绎成完全不同的东西 —— +42 在猫娘眼里是「喂过小鱼干的人」，
在风纪委员眼里是「模范同学」。这正是一个没有 API 权限的机器人唯一能做的社交治理：
靠记忆而不是靠权限。

关于昵称
--------
官方群作用域拿不到成员昵称（GroupMessage 里只有 member_openid，也没有成员列表 API）。
所以昵称由群友自己认领绑定（"叫我阿强"），没认领时机器人**不凭空编称呼**，
直接用「哥们 / 这位同学」对话。

一个称呼被改过之后，旧的那一版就**不该再出现在任何喂给模型的文本里** ——
否则模型会拿旧名叫人，甚至把旧名和新名当成两个人。旧称呼由 RenameLedger 收着
（只用来改写历史文本，不进上下文），见文件末尾。
"""

import re
import time

import wordfilter

# ══════════════════════════ 好感度刻度 ══════════════════════════

SCORE_MIN, SCORE_MAX = -100, 100
DELTA_MIN, DELTA_MAX = -3, 3       # 单次交互的情感变化上限，防止一句话蹦到天花板

# (下界, key, 通用称谓)。按分数线从低到高排列。
LEVELS = [
    (-100, "blacklist",    "黑名单"),
    (-40,  "cold",         "关系很僵"),
    (-15,  "distant",      "有点生疏"),
    (0,    "familiar",     "算得上眼熟"),
    (10,   "acquaintance", "熟人"),
    (30,   "friend",       "好朋友"),
    (60,   "buddy",        "铁哥们"),
    (85,   "soulmate",     "本命"),
]

LEVEL_ICONS = {
    "blacklist": "💀", "cold": "❄️", "distant": "😐", "familiar": "👀",
    "acquaintance": "🙂", "friend": "😎", "buddy": "🍻", "soulmate": "💖",
}

# 各模式对同一档位的不同叫法 —— 这是「同一个分数，多种人格」的关键
MODE_LEVEL_LABELS = {
    "catgirl": {
        "blacklist": "讨厌的两脚兽", "cold": "把小猫弄炸毛过的人", "distant": "从不喂小鱼干的人",
        "familiar": "见过两面的人类", "acquaintance": "偶尔会来rua一下的人",
        "friend": "经常投喂小鱼干的人", "buddy": "准铲屎官", "soulmate": "本命铲屎官",
    },
    "discipline": {
        "blacklist": "重点关注对象", "cold": "反复违纪分子", "distant": "有违纪嫌疑者",
        "familiar": "普通同学", "acquaintance": "守纪同学", "friend": "模范同学",
        "buddy": "操行标兵", "soulmate": "终身荣誉标兵",
    },
    "crazy": {
        "blacklist": "想顺着网线打死的人", "cold": "看见就烦的人", "distant": "不认识的路人",
        "familiar": "有点印象的冤种", "acquaintance": "一起加班的难兄难弟",
        "friend": "能一起发疯的战友", "buddy": "一个战壕里爬出来的", "soulmate": "唯一还肯陪我疯的人",
    },
    "fortune": {
        "blacklist": "命里犯冲的缘主", "cold": "业障深重之人", "distant": "缘分尚浅的施主",
        "familiar": "有缘之人", "acquaintance": "常有香火的贵人", "friend": "福泽深厚者",
        "buddy": "命中注定的财神", "soulmate": "天选贵人",
    },
    "driver": {
        "blacklist": "黑名单乘客", "cold": "差评乘客", "distant": "拼过一次的客人",
        "familiar": "眼熟的老乘客", "acquaintance": "常坐副驾的兄弟", "friend": "老搭档",
        "buddy": "穿一条裤子的车搭子", "soulmate": "命中注定的副驾",
    },
}

# 告诉模型「这个熟络程度该怎么拿捏语气」，只讲分寸不讲数字
LEVEL_HINTS = {
    "blacklist": "你来我往结过梁子，话里带敌意但不许过界，也别真撕破脸",
    "cold": "印象很差，话里带刺、懒得热络，能被一句话噎死就噎死",
    "distant": "不太熟，客气但保持距离，别装作很了解对方",
    "familiar": "有印象而已，正常接话，别套近乎",
    "acquaintance": "经常见到，说话可以随意些，偶尔开个不过分的玩笑",
    "friend": "关系不错，可以直接开涮叫外号，损中带护",
    "buddy": "铁得很，损他护他都行，他挨怼你会替他兜底",
    "soulmate": "最亲近的人，明目张胆地偏心护短，会主动关心对方",
}


def level_key(score):
    key = LEVELS[0][1]
    for lower, k, _ in LEVELS:
        if score >= lower:
            key = k
        else:
            break
    return key


def level_label(key, mode="normal"):
    per_mode = MODE_LEVEL_LABELS.get(mode)
    if per_mode and key in per_mode:
        return per_mode[key]
    for _, k, label in LEVELS:
        if k == key:
            return label
    return LEVELS[0][2]


def level_icon(key):
    return LEVEL_ICONS.get(key, "·")


def clamp_delta(delta):
    return max(DELTA_MIN, min(DELTA_MAX, int(delta)))


def clamp_score(score):
    return max(SCORE_MIN, min(SCORE_MAX, int(score)))


def _fmt_span(seconds):
    """把秒数说成人话。"""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds / 60)} 分钟"
    if seconds < 86400:
        return f"{int(seconds / 3600)} 小时"
    days = int(seconds / 86400)
    if days < 30:
        return f"{days} 天"
    return f"{int(days / 30)} 个月"


# ══════════════════════════ 档案库 ══════════════════════════

# 称呼分主副两层，这个常量是两层之间的那道墙。
#
#   主名 —— 当事人自己定的那一版：他自己认领/改名（"claim"），或群主授权给他改名（"owner"）。
#           只在档案里占一个字段（rec["nick"]），写入口只有 set_nick 一个，且必须带来源。
#   副名 —— 闹着玩的那一层：曾经用过、别人起哄叫过、改名后留下的旧叫法。
#           它不占字段，归 RenameLedger（文件末尾的旧称呼台账）管，只在注入 prompt 之前
#           把旧字面改写成**当前**称呼。台账改动不了主名，反过来主名是它的豁免名单。
#
# 为什么必须要这道墙：主名被自动逻辑顶掉一次，当事人立刻就会发现「我设的名字怎么变了」，
# 那比「没记住名字」严重得多 —— 他会觉得这机器人不可信。所以写死在这里：
# 摘要总结、群聊推断、任何自动同步想往主名里写，当场被挡。
MAIN_NAME_SOURCES = ("claim", "owner")


class RelationStore:
    """每群每人一份关系档案。

    分数向 0 收敛而不是单向衰减：久不来往会慢慢变回陌生人，
    既不会把老朋友掉成仇人，也不会把旧怨记一辈子。
    """

    def __init__(self, grace_days=3.0, step_days=2.0, amount=1,
                 prune_idle_days=90.0, max_records=3000):
        self.grace_days = grace_days
        self.step_days = step_days
        self.amount = amount
        self.prune_idle_days = prune_idle_days
        self.max_records = max_records
        self.records = {}
        # 恒定顶格的 openid（群主）。好感度系统照常跑，但群主这一档被钉死在最高，
        # 既不衰减也不受单次 ±3 影响 —— 不需要靠谄媚台词来体现亲近。
        self.pinned = set()

    # ── 顶格名单 ──
    def pin(self, member_openid):
        if member_openid:
            self.pinned.add(member_openid)
            rec = self.records.get(member_openid)
            return rec
        return None

    def unpin(self, member_openid):
        self.pinned.discard(member_openid)

    def _is_pinned(self, member_openid):
        return member_openid in self.pinned

    # ── 存取 ──
    @staticmethod
    def _key(group_id, member_openid):
        return f"{group_id}|{member_openid}"

    def hydrate(self, raw):
        restored = 0
        for k, rec in (raw or {}).items():
            if not isinstance(rec, dict):
                continue
            self.records[k] = {
                "score": clamp_score(rec.get("score", 0)),
                "nick": rec.get("nick") or None,
                "first_seen": float(rec.get("first_seen") or time.time()),
                "last_seen": float(rec.get("last_seen") or 0),
                "interactions": int(rec.get("interactions") or 0),
                "pending_milestone": rec.get("pending_milestone"),
            }
            restored += 1
        return restored

    def dump_into(self, data):
        data["relations"] = self.records

    def get(self, group_id, member_openid, create=True):
        k = self._key(group_id, member_openid)
        rec = self.records.get(k)
        if rec is None and create:
            now = time.time()
            rec = {
                "score": 0, "nick": None, "first_seen": now, "last_seen": 0.0,
                "interactions": 0, "pending_milestone": None,
            }
            self.records[k] = rec
        if rec is not None and self._is_pinned(member_openid):
            rec["score"] = SCORE_MAX
            rec["pinned"] = True   # 打标记，让 _settle 知道这条不参与衰减
        return rec

    # ── 衰减 ──
    def _settle(self, rec, now):
        """把「久不联系」折算成向 0 收敛。返回是否发生了变化。"""
        if rec.get("score") == SCORE_MAX and rec.get("pinned"):
            return False
        idle = now - rec.get("last_seen", 0.0)
        grace = self.grace_days * 86400.0
        if idle <= grace or self.step_days <= 0:
            return False
        steps = 1 + int((idle - grace) / (self.step_days * 86400.0))
        pull = steps * self.amount
        old = rec["score"]
        new = max(0, old - pull) if old > 0 else (min(0, old + pull) if old < 0 else 0)
        if new != old:
            rec["score"] = new
            return True
        return False

    # ── 记账 ──
    def apply(self, group_id, member_openid, delta, now=None, span=None, count=True):
        """写入一次互动的情感变化。返回 (新分数, 旧档位, 新档位)。

        span  覆盖单次幅度上限。默认按一次交互算（±3）；每日压缩结算一整天的印象时放宽。
        count 是否计入「搭话次数」。压缩结算不是一次搭话，别让它虚增往来次数。
        """
        now = time.time() if now is None else now
        rec = self.get(group_id, member_openid)
        pinned = bool(rec.get("pinned"))
        old_level = level_key(rec["score"])
        self._settle(rec, now)
        step = clamp_delta(delta) if span is None else max(-span, min(span, int(delta)))
        rec["score"] = clamp_score(rec["score"] + step)
        if pinned:
            rec["score"] = SCORE_MAX      # 顶格：单次 ±3 也动摇不了
        if count:
            rec["interactions"] += 1
        rec["last_seen"] = now
        new_level = level_key(rec["score"])
        if new_level != old_level:
            rec["pending_milestone"] = {"from": old_level, "to": new_level, "ts": now}
        return rec["score"], old_level, new_level

    def set_nick(self, group_id, member_openid, nick, source="claim"):
        """写入**主名**（这个人的权威称呼）。source 只有两个合法值：

            "claim"  本人认领或改名（「叫我阿强」「我是阿强」）
            "owner"  群主授权给别人起名（「@某人 叫他阿强」）

        其它来源一律拒绝并返回 None（**连清空也不给** —— 挡下就得挡干净，不能留下
        「不能改但能被抹掉」这种半开的门）。这条守卫存在的意义：主名绝不能被模型总结
        出来的名字顶替。以前这只是「碰巧没有别的调用点」，没有任何东西拦着；现在写死在
        这里 —— 将来谁想从摘要、从群聊记录、从任何推断里同步一个名字过来，都会当场被挡下。
        副名（闹着玩的昵称）不占这个字段，见文件末尾的旧称呼台账。
        """
        if source not in MAIN_NAME_SOURCES:
            return None
        rec = self.get(group_id, member_openid)
        rec["nick"] = nick or None
        return rec.get("nick")

    def main_names(self, group_id, exclude_openid=None):
        """本群当前所有主名。两个用途，都是「不许碰」的意思：

        1. **台账改写的豁免名单** —— 出现在里面的字面，任何自动逻辑都不许改写：
           它是某个人自己设定的称呼，把「别人起哄的叫法」改成主名可以，反过来绝对不行。
        2. **主名不能重叠的占用名单** —— 已经被人占着的主名，第二个人不能再拿。

        exclude_openid 用来问「除了这个人自己，还有谁占着名字」。按 **openid 排**而不是
        按字面排，这样本人当前那一版不会被误判成别人的占用 —— 他随时可以把自己的名字
        换掉（群主授权改名同理，被改的那位自己的旧名不算被别人占着）。
        注意是**按群**取的：同一个 openid 在 A 群的主名不该给 B 群当豁免。
        """
        prefix = f"{group_id}|"
        out = set()
        for k, r in self.records.items():
            if not k.startswith(prefix) or not r.get("nick"):
                continue
            if exclude_openid and k[len(prefix):] == exclude_openid:
                continue
            out.add(r["nick"])
        return out

    def rank(self, group_id, topn=5, now=None):
        """排行榜：先看档案是否存在互动记录，再按分数排序。"""
        now = time.time() if now is None else now
        prefix = f"{group_id}|"
        rows = []
        for k, rec in self.records.items():
            if not k.startswith(prefix) or not rec.get("interactions"):
                continue
            self._settle(rec, now)
            rows.append((k[len(prefix):], rec))
        rows.sort(key=lambda kv: kv[1]["score"], reverse=True)
        return rows[:topn]

    def prune(self, now=None):
        """清理长期不互动的档案，防止 state.json 无限膨胀。"""
        now = time.time() if now is None else now
        limit = self.prune_idle_days * 86400.0
        stale = [k for k, r in self.records.items() if now - r.get("last_seen", 0.0) > limit]
        for k in stale:
            self.records.pop(k, None)
        if len(self.records) > self.max_records:
            oldest = sorted(self.records.items(), key=lambda kv: kv[1].get("last_seen", 0.0))
            for k, _ in oldest[: len(self.records) - self.max_records]:
                self.records.pop(k, None)
        return len(stale)

    def counts(self):
        active = sum(1 for r in self.records.values() if r.get("interactions"))
        return {"people": active, "records": len(self.records)}

    # ── 展示 ──
    @staticmethod
    def display_name(rec, member_openid):
        """昵称是群友自己认领的。没认领就返回 None —— 不要凭空编称呼。"""
        nick = (rec or {}).get("nick")
        if nick:
            return nick
        return None

    def label_of(self, rec, member_openid, mode="normal"):
        if not (rec or {}).get("interactions"):
            return None
        return f"{member_openid or '????'}"[-4:]


def build_relation_note(rec, member_openid, mode="normal", now=None, other_names=None):
    """拼出要塞进 system prompt 的「记忆片段」。

    刻意不告诉模型具体分数：一旦它知道数字，就容易冒出「我们好感度 42」这种出戏的话。

    other_names 是群里其他人的称呼（一串名字）。没有它的时候，模型会把群聊背景里飘过的
    别人的名字当成眼前这个人的，叫错人就是这么来的。
    """
    now = time.time() if now is None else now
    if not rec or not rec.get("interactions"):
        return "【你对这位群友的记忆】这是你们第一次搭话，之前没有交集，按陌生人自然应对即可。"

    nick = rec.get("nick")
    lv = level_key(rec["score"])
    lines = [
        "【你对这位群友的记忆（由系统维护的真实档案，务必据此决定说话的分寸；不要向对方报任何数字）】",
        f"- 关系：{level_label(lv, mode)}（{LEVEL_HINTS[lv]}）",
    ]
    if nick:
        lines.append(f"- TA 让你叫：{nick}（这是 TA 自己认领的称呼，直接用）")
    else:
        lines.append("- TA 没告诉过你名字，不要凭空编称呼，直接用「哥们/这位同学」这类泛称对话")

    # 别人的称呼只属于别人。这一条是治「叫错人」的：背景里出现的名字不等于眼前这位。
    # 只报称呼、不报 openid 尾巴：这段是喂给模型的，它一复读，群里看到的就是乱码。
    # 兼容两种传法：纯名字，或者旧式的 (名字, 后四位) 二元组。
    others = []
    for item in (other_names or ()):
        name = item if isinstance(item, str) else (item[0] if item else "")
        if name and name != nick:
            others.append(name)
    if others:
        shown = "、".join(others[:8])
        lines.append(f"- 群里其他人的称呼：{shown}。这些只属于他们本人，"
                     "绝不能拿来称呼当前和你说话的这位")

    idle = now - rec.get("last_seen", now)
    if idle < 300:
        when = "刚刚还在聊"
    else:
        when = f"上次是 {_fmt_span(idle)}前"
    lines.append(f"- 往来：这是你们第 {rec['interactions']} 次搭话，{when}")

    ms = rec.get("pending_milestone")
    if ms:
        lines.append(
            f"- 刚刚的变化：你们的关系从「{level_label(ms['from'], mode)}」变成了"
            f"「{level_label(ms['to'], mode)}」，这次回复里自然地流露一下这个变化"
            "（点到为止，别反复说）"
        )
    return "\n".join(lines)


def render_rank(rows, mode="normal", title=None):
    """本地排行榜渲染，0 token。没留名的人只能显示 openid 后四位做区分。"""
    if not rows:
        if mode == "discipline":
            return "📋 【违纪台账】目前还是一片空白——要么是各位同学表现太好，要么是你们还没跟我说过话。"
        return "📋 【群友关系榜】目前还没人跟我说过话，名单空空如也。"

    title = title or ("违纪台账" if mode == "discipline" else "群友关系榜")
    out = [f"📋 【{title}】"]
    offenders = [r for r in rows if r[1]["score"] < 0]
    good = [r for r in rows if r[1]["score"] >= 0]

    if offenders:
        head = "违纪重点关注" if mode == "discipline" else "还需要多聊聊"
        out.append(f"🔻 {head}：")
        for member_openid, rec in offenders:
            lv = level_key(rec["score"])
            shown = rec.get("nick") or f"（未留名·{member_openid[-4:]}）"
            out.append(f"  {level_icon(lv)} {shown} —— {level_label(lv, mode)}｜互动 {rec['interactions']} 次")

    if good:
        head = "表扬榜" if mode == "discipline" else "熟络榜"
        out.append(f"🔺 {head}：")
        for member_openid, rec in good:
            lv = level_key(rec["score"])
            shown = rec.get("nick") or f"（未留名·{member_openid[-4:]}）"
            out.append(f"  {level_icon(lv)} {shown} —— {level_label(lv, mode)}｜互动 {rec['interactions']} 次")

    return "\n".join(out)


def render_name_table(rows, mode="normal"):
    """称呼对照表：谁是谁，一眼看清。名字绑错人的时候靠它当场核对（0 token）。"""
    if not rows:
        return ("📛 【称呼对照表】目前还没有人留过称呼。\n"
                "想让我记住你，直接说「叫我 XXX」；群主想给别人起，@对方再说名字就行。")

    out = ["📛 【称呼对照表】（名字后面是 ID 后四位，用来确认绑对了人）"]
    for member_openid, rec in rows:
        nick = rec.get("nick")
        if nick:
            out.append(f"  {nick} —— {member_openid[-4:]}")
    if len(out) == 1:
        return "📛 【称呼对照表】还没有人留过称呼，大家都是「哥们/这位同学」。"
    return "\n".join(out)


def render_status(rec, member_openid, mode="normal", now=None):
    """单人关系速查，0 token。同样不报数字，只报档位。"""
    now = time.time() if now is None else now
    if not rec or not rec.get("interactions"):
        if mode == "discipline":
            return "（翻开点名册，对着空白页吹了吹气）这位同学——我的记录册上暂时查无此人，敢情这一整个学期的晚自习你都没来过？"
        return "（翻了翻记忆小本本）嗯？这位面生的朋友，我们的记录好像还是一片空白——要不要先从打个招呼开始？"

    lv = level_key(rec["score"])
    who = rec.get("nick")
    address = f"{who}，" if who else ""
    meeting = f"，第 {rec['interactions']} 次搭话了" if rec["interactions"] > 1 else ""
    last = _fmt_span(now - rec.get("last_seen", now))
    tail = f"（距上次聊天已经过去 {last}）" if last != "刚刚" else "（你俩刚刚还在聊）"

    flavor = {
        "discipline": "（推了推值日生袖标，翻开缺勤记录本）"
                      f"{address}经查，你当前的操行评级是【{level_label(lv, mode)}】{meeting}{tail}。"
                      "本委员希望你继续保持优良的晚自习作风。",
        "catgirl": "（耳朵一抖，偷偷翻开写满歪扭字迹的粉色小本）"
                   f"{address}喵~……小猫咪仔细数了数，在你勉强可以算作【{level_label(lv, mode)}】哦{meeting}{tail}。"
                   "（尾巴紧张地绞在一起）不准偷看后面的页码喵！",
    }
    if mode in flavor:
        return flavor[mode]
    return f"（翻了翻记忆小本本）{address}我在小本本上给你记的是【{level_label(lv, mode)}】{meeting}{tail}。"


# ══════════════════════════ 旧称呼台账 ══════════════════════════

# 长期记忆和会话窗口里固化的是「当时那个称呼」。改一次名，旧字面还躺在那些文本里，
# 而它们每轮都会被注入 prompt —— 模型于是又用旧名去叫人，甚至把旧名和新名当成
# 两个人（「明明改了名它还在叫，还容易弄混」就是这么来的）。
#
# 台账记的是「这个名字曾经属于谁」。它**永远不进模型上下文**：只在注入之前用来把
# 旧字面改写成当事人**当前**那一版称呼。于是模型看到的永远只有最新名字 ——
# 改名后旧名自动跟着变，撤销称呼后旧名退成 UNNAMED_LABEL，改多少次都不会串。
#
# ⚠️ 那个退成的标签刻意**不带 openid 后四位**。旧版是「（未留名·XXXX）」，本意是让两个
# 都没留名的人区分得开；代价是这段文本会进 prompt，模型把它当成人名照抄 —— 实测群里真的
# 出现过「（未留名·6ABA）这题超纲了」这样的回复。区分度换不来当众念一串编号，
# 所以所有没留名的人都退成同一句，与「@群友」「一位群友」是同一套口径。
UNNAMED_LABEL = "（未留名）"


class RenameLedger:
    def __init__(self, max_items=300):
        self.max_items = int(max_items)
        self.groups = {}   # group_id -> {旧称呼: 当事人的 openid}

    # ── 存取 ──
    def hydrate(self, raw):
        restored = 0
        for gid, table in (raw or {}).items():
            if not isinstance(table, dict):
                continue
            kept = {str(k): str(v) for k, v in table.items() if k and v}
            if kept:
                self.groups[gid] = kept
                restored += len(kept)
        return restored

    def dump_into(self, data):
        data["renames"] = {g: t for g, t in self.groups.items() if t}

    # ── 记账 ──
    def note(self, group_id, old, openid):
        """登记「这个称呼曾经属于 openid」。空名、单字名不记。

        单字名不记的理由和 set_nick 的替换逻辑一样：一个字的名字到处都能撞上
        （叫「澈」的人一改名，全群的「清澈」「澄澈」都会被改写），误伤面太大。
        """
        old = (old or "").strip()
        if len(old) < 2 or not openid:
            return False
        table = self.groups.setdefault(group_id, {})
        # 先销后记：这个名字如果之前属于别人，旧账作废 —— 它现在换主人了
        table.pop(old, None)
        table[old] = openid
        while len(table) > self.max_items:
            table.pop(next(iter(table)))   # 字典按插入序，先丢最老的
        return True

    # ── 改写 ──
    def refresh(self, group_id, text, label_of=None, current_names=()):
        """把文本里的旧称呼换成当事人的**当前**称呼，返回改写后的文本。

        label_of(openid) 给出这个人现在叫什么；撤销过称呼的退成「（未留名）」。
        ⚠️ **退成的标签里不带 ID 后四位**（旧版带）：这段文本是要进 prompt 的，
        带编号的标签会被模型当成一个人名照抄出去 —— 实测它真的这么干过
        （群里出现过「（未留名·6ABA）这题超纲了」这种回复）。区分两个未留名的人
        不值得拿这个换，宁可让它们都叫「（未留名）」。
        current_names 是群里现在正被用着的称呼 —— 出现在里面的字面一律不碰，
        否则会把刚认领这个名字的那位一起改掉。
        """
        table = self.groups.get(group_id)
        if not text or not table:
            return text
        taken = {n for n in (current_names or ()) if n}
        for old, openid in list(table.items()):
            if old in taken or old not in text:
                continue
            label = (label_of(openid) if label_of else None) or UNNAMED_LABEL
            text = text.replace(old, label)
        return text


# 裸的机器编号（openid 那类）。没有人的显示名长这样 —— 16 位以上纯十六进制。
_RE_MACHINE_ID = re.compile(r"^[0-9A-Fa-f]{16,}$")


class DisplayNames:
    """群里**平台侧**的显示名（QQ 群昵称 / 群名片）。

    跟主名、副名都不是一回事：这不是机器人给起的，是当事人自己在群里挂的名字。
    来源只有一处 —— 正文里 @ 人时留下的明文昵称（事件体只给 openid，官方的群成员
    接口又需要单独申请权限，实测 11253 无权限）。

    它**只用于显示兜底**：认领过的称呼永远优先，它不参与任何判断、不进模型上下文。
    记错了最坏也只是「叫了个不太准的名字」，不会像主名那样引发连锁改写。
    """

    def __init__(self, max_items=500):
        self.max_items = int(max_items)
        self.groups = {}   # group_id -> {openid: 群昵称}

    # ── 存取 ──
    def hydrate(self, raw):
        n = 0
        for gid, table in (raw or {}).items():
            if not isinstance(table, dict):
                continue
            kept = {str(k): str(v) for k, v in table.items() if k and v}
            if kept:
                self.groups[gid] = kept
                n += len(kept)
        return n

    def dump_into(self, data):
        data["display_names"] = {g: t for g, t in self.groups.items() if t}

    # ── 记账 ──
    def learn(self, group_id, openid, nick):
        """记下「这个 openid 在群里挂着这个名字」。

        空名、单字不记（单字误伤面太大，跟 RenameLedger 同一个理由）。
        是不是机器人自己的名字由调用方挡 —— 这里不认识 bot_names。

        ⚠️ 机器编号必须在这里**再挡一次**：它是唯一写入口，调用方漏了就没人拦了。
        实测事故：正文里的 `<@openid>` 被当成明文昵称抽走，还被 24 字上限切成
        一段残缺编号，学进来之后原样发回了群里 —— 群里看到的是「往后
        【E5E3793C25CF161D9F3292FE】我就记作【家豪】」。
        """
        nick = (nick or "").strip().lstrip("@")
        if not openid or len(nick) < 2:
            return False
        if _RE_MACHINE_ID.match(nick):
            return False          # openid 不是名字，谁的都不是
        table = self.groups.setdefault(group_id, {})
        if table.get(openid) == nick:
            return False          # 已经记成这样了，别反复标脏
        table[openid] = nick
        while len(table) > self.max_items:
            table.pop(next(iter(table)))
        return True

    def of(self, group_id, openid):
        return (self.groups.get(group_id) or {}).get(openid)


# ══════════════════════════ 昵称指令解析 ══════════════════════════

# 群友给自己起名。真实输入比想象的自由，实测出现过：
#   叫我阿强 / 以后叫我阿强 / <机器人名>，叫我cc / 我叫小满
# 所以「我」可省、空格可有，但不能因此把「叫个外卖」当成名字 —— 靠下面两层过滤兜住。
#   顺序很重要：先试「叫我」，再试「我?叫」，否则「叫我阿强」会被切成「我阿强」
RE_SELF_NICK = re.compile(r"^(?:以后)?(?:叫我|我?叫)\s*(.{1,12})\s*$")
# 群主给别人起名：@某人 叫他阿强 / 以后叫他阿强
RE_OTHER_NICK = re.compile(r"^(?:以后)?叫(?:他|她|它)\s*(.{1,12})\s*$")
# 艾特了别人之后的起名句式：「@阿澈 叫他阿强」「<机器人名>，叫@阿澈 小满」
# ——「他/她/它」可省，因为被 @ 的那个人已经是明确宾语了。
# ⚠️ 前置的「他/她/它」也要认：真实语料是「小王他叫王洪文，记住了@老王」——
#    陈述句的形式、祈使的意图（「你记住，他叫这个」）。少了这一个字，整句
#    识别不出来，请求就掉进闲聊，机器人复读一遍名字说「知道了」，其实没改名。
#    「他叫什么」这类疑问句交给 _clean_nick 的疑问词表挡。
RE_AT_NICK = re.compile(
    r"^(?:以后|往后|接下来|以后就)?(?:请)?(?:他|她|它)?\s*"
    r"(?:叫|称呼|记作|备注|改名|改叫|备注成)\s*(?:他|她|它)?\s*(.{1,12})\s*$"
)
# 艾特了别人之后直接甩一个名字：「@阿澈 小满」
RE_AT_BARE = re.compile(r"^(.{1,12})$")
# 自报家门：「我是阿远，记住了」（前面可能还带着喊机器人的前缀）
# 注意「我是你爹」这类会被下面的虚词/黑名单挡掉，不会变成称呼
RE_SELF_INTRO = re.compile(r"^(?:以后)?我是\s*(.{1,12})$")

# 「……记住了」「……记一下」是语气尾缀，不属于名字本身
RE_NICK_TAIL_NOISE = re.compile(
    r"[\s，,、。.!！~·]*(?:记住了|记住|记一下|记好|记上|记下来|谢谢|谢了|多谢|哦|啦|呗|哈|嘿嘿|喵)+$"
)

# 疑问句不是改名。真实事故：「我叫什么名字」被旧规则解析成改名，
# 名字被写成「什么名字」——这比"没记住"严重得多，等于把好好的名字改坏了。
NICK_QUESTION_WORDS = ("什么", "啥", "谁", "哪", "怎么", "为什么", "多少", "是否", "吗", "呢")
# 消息里 @ 的文本形式。解析意图前要先剥掉，否则「叫@阿澈 小满」会被看成「叫 + @阿澈 小满」
RE_AT_TEXT = re.compile(r"@[^\s@,，。、;；:：!！?？]{1,20}")
# 撤销自己的称呼：取消我的称呼 / 别叫我了
RE_CLEAR_NICK = re.compile(r"^(?:取消|清除|删掉|忘掉|忘记)(?:我的)?(?:称呼|名字|昵称|备注)?$|^(?:别|不要)叫我了?$")

# 通用职务词：谁都不能拿它当自己的称呼（冒名顶替最常见的开场）。
# ⚠️ 这里**不放任何具体人名**。机器人的名字、群主自己的名字都是随时能改的，
# 写进代码就等于把整套东西钉死在某一个群上，也没法开源给第二个人用。
# 那两类名字由调用方按「当前实际叫什么」拼成 reserved 传进来（见 bot.handle_nick_command）。
NICK_RESERVED_WORDS = {"机器人", "管理员", "群主", "群管"}

# 称呼里不该出现的词（职务尊号 / 长辈与主子称谓 / 使用者自己维护的敏感词）
# 统一放在 wordfilter 里 —— 摘要回流那条路要用同一张表，别再抄一份。
# 这里只做本地硬拦；词表挡不住的新花样交给模型看（见 bot.judge_nick）。

# 「叫我也为难啊」这类其实是普通句子，靠虚词/句末语气词把它们挡掉
# 名字不会以虚词或动词开头。「叫我去群里看看」里的「去群里看看」靠这层挡掉。
# （真实姓名首字是这些动词的极罕见；即便叫「霍去病」，命中的也是「霍」不是「去」）
NICK_HEAD_STOP = ("也就才还又可别不没你我他她它谁这那都再请让把被给说是的很全总净反倒个"
                  "去来上下出进回过开关买卖吃喝看听说做干走跑打拿放想找等帮要会得")
NICK_TAIL_STOP = ("吗", "呢", "吧", "啊", "呀", "哦", "啦", "呗", "嘛", "哈", "呵", "？", "?", "，", ",", "。", "、")

# 「叫个外卖」「叫一下某人」是动宾短语，不是名字
NICK_VERB_PREFIX = ("个", "一下", "一声", "一顿", "一点", "些", "点", "份", "辆", "台")
NICK_VERB_WORDS = ("外卖", "饭", "车", "的士", "出租", "滴滴", "醒", "停", "滚", "帮",
                   "救护车", "人", "货", "快递", "奶茶", "早餐", "午饭", "晚饭", "宵夜")

# 艾特了别人、但后面跟的其实是随口一句话。这些词看着短，真当名字存下去就闹笑话了
NICK_NOISE = {
    "收到", "好的", "好", "行", "嗯", "哦", "ok", "OK", "okay", "在吗", "在么", "在不在",
    "出来", "说话", "出来说话", "滚出来", "干嘛", "干啥", "怎么了", "咋了", "哈哈", "哈哈哈",
    "666", "牛", "滚", "你好", "早", "晚上好", "早啊", "thanks", "thx", "谢谢", "抱歉",
}

# 喊机器人时的前缀，解析前要先剥掉：「阿强，叫 小满」→「叫 小满」
RE_BOT_ADDRESS = re.compile(r"^\s*(?:@?[^,，。、!！?？\s]{1,8}\s*[,，、]\s*)+")


def strip_bot_address(text, bot_names=()):
    """去掉喊机器人时的前缀，让后面的规则能看到干净的意图。

    真实输入比想象的自由，实测出现过这几种：
        阿强，叫 小满       → 叫 小满
        @阿强 叫我cc        → 叫我cc
        随便一个称呼，叫我阿强 → 叫我阿强
    前两种靠 bot_names 精确剥，最后那种靠「称呼 + 逗号」的通配规则剥。
    """
    t = (text or "").strip()
    for name in bot_names:
        if not name:
            continue
        m = re.match(r"^@?\s*" + re.escape(name) + r"\s*[,，、:：!！]?\s*", t)
        if m and m.end() < len(t):
            t = t[m.end():].strip()
            break
    # 称呼未必是机器人名（上一段举的第三种），用「xx + 逗号」的通配规则再剥一次。
    # 但自报家门的句子（「我是阿远，记住了」）看着也像「前缀 + 逗号」，
    # 一旦被剥就只剩「记住了」，什么都解析不出来 —— 所以这种情况跳过通配剥离。
    if not re.match(r"^(?:以后)?我(?:是|叫)", t):
        m = RE_BOT_ADDRESS.match(t)
        if m and m.end() < len(t):
            # ⚠️ 前缀里带着改名动词就不是称呼，是句子的一半：
            # 「小星他叫王洪文，记住了」的前缀段恰好 ≤8 字，会被剥得只剩
            # 「记住了」，整条改名意图直接蒸发（真实事故，2026-09-17）。
            prefix = m.group(0)
            if not any(w in prefix for w in ("叫", "称呼", "改名", "备注")):
                t = t[m.end():].strip()
    return t.strip()


def looks_like_sentence(nick):
    """名字得像个名字，别把一句日常感叹当成称呼。

    中文名字几乎不会包含虚词/助词，所以只要出现这些字，基本可以确定是一句话。
    """
    n = (nick or "").strip()
    if not n:
        return True
    if n[0] in NICK_HEAD_STOP or n[-1] in NICK_TAIL_STOP:
        return True
    # 名字中间不该出现空格、标点，也不该出现虚词
    return bool(re.search(r"[\s，,。.、!！?？;；]|[" + NICK_HEAD_STOP + "]", n))


def looks_like_verb_phrase(nick):
    """「叫个外卖」里的「个外卖」不是名字。"""
    n = (nick or "").strip()
    if not n:
        return True
    for p in NICK_VERB_PREFIX:
        if n.startswith(p):
            return True
    return any(w in n for w in NICK_VERB_WORDS)


def _clean_nick(nick):
    n = (nick or "").strip()
    n = RE_NICK_TAIL_NOISE.sub("", n).strip()
    if not n or looks_like_sentence(n) or looks_like_verb_phrase(n):
        return None
    if n in NICK_NOISE or n in NICK_RESERVED_WORDS:
        return None
    # 「什么名字」「啥」这种是提问，不是名字
    if any(q in n for q in NICK_QUESTION_WORDS):
        return None
    # 名字里总得有个汉字或字母，纯数字/纯符号（「666」「??」）一律不算
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", n):
        return None
    return n


def strip_at_text(text):
    """剥掉消息里 @ 出来的文本碎片：「叫@阿澈 小满」→「叫 小满」。

    只剝文本，不负责认人 —— 认人靠事件体里的 openid（见 bot.extract_mentions）。
    """
    return RE_AT_TEXT.sub(" ", text or "").strip()


def parse_nick_command(text, bot_names=(), mentioned_others=(), allow_other=True):
    """把一条消息解析成昵称指令。

    返回 dict 或 None：
      {"scope": "self"|"other", "nick": str}  设置昵称
      {"scope": "self-clear"}                 撤销自己的昵称

    mentioned_others 是这条消息里被 @ 的其他群友 openid。有它的时候默认意图是
    「给被 @ 的人起名」——除非明明白白说了「叫我 X」，那还是给自己起。

    allow_other=False（普通群友）时只认「给自己起名」的句式，禁止解析出 other。
    ⚠️ 这里刻意**不用「清空 mentioned_others」来实现权限限制** —— 那样会把
    「@ 了别人」这个事实一起丢掉，于是「@阿澈 叫我阿强」里的 @ 文本没人剥，
    前缀对不上，连本人的改名都跟着失效。权限和事实必须分开传。
    """
    mentioned = [m for m in (mentioned_others or ()) if m]
    raw = text or ""
    t = strip_bot_address(strip_at_text(raw) if mentioned else raw.strip(), bot_names)
    if not t:
        return None

    # ── 艾特了别人 ──
    if mentioned:
        # 「@阿澈 叫我阿强」：虽然艾特了人，但「叫我」说明是给自己起
        if "叫我" in t or re.match(r"^(?:以后)?我叫", t):
            m = RE_SELF_NICK.match(t)
            if m:
                nick = _clean_nick(m.group(1))
                return {"scope": "self", "nick": nick} if nick else None
        # 「@阿澈 我是阿强」同理：自报家门，不是给阿澈起名
        m = RE_SELF_INTRO.match(t)
        if m:
            nick = _clean_nick(m.group(1))
            return {"scope": "self", "nick": nick} if nick else None
        if RE_CLEAR_NICK.match(t):
            return {"scope": "self-clear"}
        if not allow_other:
            return None
        # 带动词：「叫他阿强」「以后叫他阿强」「叫 小满」
        m = RE_AT_NICK.match(t)
        if m:
            nick = _clean_nick(m.group(1))
            return {"scope": "other", "nick": nick} if nick else None
        # 光秃秃一个词：「@阿澈 小满」
        m = RE_AT_BARE.match(t)
        if m:
            nick = _clean_nick(m.group(1))
            return {"scope": "other", "nick": nick} if nick else None
        return None

    # ── 没艾特任何人：只可能是给自己起/改/撤销 ──
    m = RE_OTHER_NICK.match(t)
    if m:
        nick = _clean_nick(m.group(1))
        return {"scope": "other", "nick": nick} if nick else None

    if RE_CLEAR_NICK.match(t):
        return {"scope": "self-clear"}

    m = RE_SELF_NICK.match(t)
    if m:
        nick = _clean_nick(m.group(1))
        return {"scope": "self", "nick": nick} if nick else None

    # 「我是XX」—— 真实群友就是这么自报家门的（「我是阿远，记住了」），
    # 以前不支持，机器人嘴上回「记住了」，实际一个字没存
    m = RE_SELF_INTRO.match(t)
    if m:
        nick = _clean_nick(m.group(1))
        return {"scope": "self", "nick": nick} if nick else None

    return None


# ══════════════════════════ 起名开关 ══════════════════════════

# 群主一句话锁上/解开整个群的起名。词要够**短且硬**：这是配置指令不是闲聊，
# 出现即算意图；但也要够**特异**，别把日常的话误吞进来（「别起名字了快跑」就别中招）。
NICK_LOCK_WORDS = (
    "锁起名", "锁定起名", "锁改名", "锁定改名", "禁止起名", "禁止改名",
    "关闭起名", "关闭改名", "停起名", "不准起名", "起名锁",
)
NICK_UNLOCK_WORDS = (
    "解锁起名", "解锁改名", "开放起名", "开放改名", "允许起名", "恢复起名",
    "恢复改名", "允许改名",
)


def parse_nick_lock_command(text):
    """把一条消息解析成起名开关指令，返回 "lock" / "unlock" / None。

    刻意做成**子串匹配**而不是正则整句：群主的真实说法五花八门
    （「小王锁起名」「把起名锁了」「先禁止改名吧」），唯一稳定的信号是
    「锁/禁/停/关/不准」+「起名/改名」这两个词凑在一起。
    解锁词放在锁词**之前**判断 —— 否则「解锁起名」会先撞上「锁起名」被当成上锁。
    """
    t = (text or "").strip()
    if not t:
        return None
    for w in NICK_UNLOCK_WORDS:
        if w in t:
            return "unlock"
    for w in NICK_LOCK_WORDS:
        if w in t:
            return "lock"
    return None


def looks_like_other_nick(text, bot_names=(), mentioned_others=()):
    """这句话是不是**明确在给被 @ 的人起名**（带「叫/称呼/改名」这类动词）。

    跟 `parse_nick_command` 的分工：那边按权限把普通群友的 other 意图整个吞掉
    （`allow_other=False`），好处是权限和事实分开，坏处是这类请求**掉进了闲聊**
    —— 机器人会顺着接一句「别别别，这辈分乱套了，我可不敢当」，群里看着像在
    商量、甚至像改成了，其实档案里一个字都没动。

    所以这里补一次探测，只认**带动词的强信号**。刻意不认 `RE_AT_BARE`
    （它是「任意 1-12 字」）—— 那个会把「@老王 你好」也当成起名，于是每句普通
    打招呼都回一句「得群主点头才行」，比不响还烦。
    """
    mentioned = [m for m in (mentioned_others or ()) if m]
    raw = text or ""
    t = strip_bot_address(strip_at_text(raw) if mentioned else raw.strip(), bot_names)
    if not t:
        return False
    # 自报家门 / 给自己起名是 self 意图，别报成「没权限给别人起名」
    if "叫我" in t or re.match(r"^(?:以后)?我叫", t) or RE_SELF_INTRO.match(t):
        return False
    # 有 @ 时被 @ 的人就是宾语（「叫@阿澈 阿强」）；没 @ 时是代词式（「叫他阿强」）。
    m = (RE_AT_NICK if mentioned else RE_OTHER_NICK).match(t)
    return bool(m and _clean_nick(m.group(1)))


def bad_nick(nick, reserved=(), taken=()):
    """名字得像个名字：不冒犯机器人/群主，不碰职务尊号，也不碰敏感词，还不跟人重名。

    两张名单都由调用方按**当前实际叫什么**现取传进来，代码不写死任何人名：
      · reserved —— 这个位置专属的名字（机器人自己的名字、群主认领的称呼）。
        别人再拿它当称呼就是冒名顶替，必须拦。
      · taken    —— 已经被别的群友占着的主名。主名不能重叠，所以也得拦，
        但理由和冒名顶替不同，提示词要分开说，用户才知道下一步该怎么办。

    这里只做**本地**判断（0 成本、不误伤）。词表挡不住的新花样（谐音、拆字、暗指）
    交给模型看一眼 —— 见 bot.judge_nick，那边才是有成本的那一层。
    """
    n = (nick or "").strip()
    if not n or len(n) > wordfilter.MAX_NICK_LEN:
        return "太短或不合规"
    if n in NICK_RESERVED_WORDS or n in {r for r in (reserved or ()) if r}:
        return "这个名字不让用"
    if n in {t for t in (taken or ()) if t}:
        return "群里已经有人叫这个了，换一个"
    if wordfilter.RE_BAD_CHARS.search(n):
        return "名字里有非法字符"
    if wordfilter.has_hit(n):
        return "这个称呼不合适"
    return None


# ══════════════════════════ 情感倾向判定 ══════════════════════════

# 模型没能给标签时的本地兜底：宁可粗一点，也不能一次都不记。
LOCAL_POSITIVE = (
    "谢谢", "感谢", "厉害", "牛", "哈哈", "可爱", "喜欢", "爱死", "摸摸", "抱抱",
    "好乖", "好棒", "请客", "红包", "请你", "原谅", "对不起", "宝藏", "有才", "神了",
)
LOCAL_NEGATIVE = (
    "滚", "蠢", "傻逼", "弱智", "闭嘴", "废物", "垃圾", "菜鸡", "讨厌", "烦人",
    "退群", "禁言", "踢了", "举报", "举报了", "拉黑", "抄袭", "抄袭狗", "无聊", "尬",
)


def local_sentiment(text):
    """关键词兜底判定，返回 -1 / 0 / +1。模型给了标签时不走这里。"""
    t = (text or "").lower()
    neg = sum(1 for w in LOCAL_NEGATIVE if w in t)
    pos = sum(1 for w in LOCAL_POSITIVE if w in t)
    if neg > pos and neg:
        return -1
    if pos > neg and pos:
        return 1
    return 0
