"""QQ 消息内容规范化：留下人能读的，丢掉读不出的。

message.content 是「人话 + 机器占位符」混排的一坨，直接拿去用会污染三处：
归档原文、群聊背景缓存、喂给模型的上下文。三种典型噪声：

    <@!A1B2C3D4>                                           @ 某人，只有 openid
    <faceType=1,faceId="0",ext="eyJ0ZXh0Ijoi5oOK6K62In0=">  表情，语义全在 ext 里
    [附件1] 类型:图片 文件名:x.jpg URL:https://...           附件元数据

normalize() 把它们收敛成人话：能解的（被 @ 的人、表情配文）换成短标记，解不出的
（空表情、附件元数据、裸链接）直接丢掉。返回值可能为空串 —— 调用方据此知道这条
消息压根没有可留存的内容，整条丢弃即可。

@ 为什么必须留下
----------------
「谁被点名了」是这句话里最关键的信息，清掉等于把事实一起扔掉：实测群友 @ 群主
问「这是谁」，机器人那边什么都没收到，只能回一句「我不知道你 @ 的是谁」。
所以 @ 会渲染成「@某人」，具体是谁由调用方通过 mention_label 注入
（qqtext 不认识群友 —— 认人靠关系档案，那是上层的事）；解析不出来时退成
「@群友」，仍然看得出「这里 @ 了个人」（**不附带 openid 尾巴**）。

怎么扩展
--------
加一种占位符 = 用 @renderer 注册一个函数，不用碰解析逻辑：

    @renderer("img", "image")
    def _render_image(attrs):
        return "[图片]"

占位符有两种形态，分别处理：
  · 属性型 <faceType=1,ext="..."> → 走 RENDERERS 注册表，键取第一个属性名
  · 位置型 <@!openid>              → 走 _MENTION，它没有 k=v，硬塞进属性表只会更难读
刻意不在这里写「碰到 faceType 或 img 或 url 就……」这类枚举正则 —— 那样每来一个
新格式都要回来改解析，格式一多变就是一团浆糊。
"""

import base64
import json
import re

# 占位符容器：<...>。QQ 的结构化占位符都在尖括号里且内容不含尖括号，所以解析
# 不需要知道里面是什么类型 —— 类型判断交给下面的渲染器注册表。
_TAG = re.compile(r"<([^<>]{1,4000})>")

# @ 某人：<@!openid> 或 <@openid>。@ 后面的 id 是位置参数，不是属性。
# 前面的 "@?" 是故意的：真实内容里这两种写法会连在一起（「刚 @<@!xxx> 说的」），
# 不把那个多余的 @ 一起吃掉，正文里就会留下「@@某人」。
_MENTION = re.compile(r"@?<@!?([^<>]{1,64})>")

# 附件描述是系统生成的一整行元数据。前缀已经足够特异，直接吃到行尾，
# 不为它枚举「类型/文件名/尺寸/大小/URL」这些属性名。
_MEDIA_LINE = re.compile(r"\[附件\d*\][^\n]*")

# 裸链接：读不出内容又长，留着只会挤占上下文。
_URL = re.compile(r"https?://\S+")

# 占位符内部的属性串：faceType=6,faceId="0",ext="xxx"
_ATTR = re.compile(r'([A-Za-z_]\w*)=(?:"([^"]*)"|([^",]*))')

_WS = re.compile(r"[ \t\u3000]+")
_BLANK = re.compile(r"\n{3,}")

# 没有解析器时，或者解析器也认不出这个 openid 时，用这个兜底（不含 @ 前缀）。
# 认不出是谁时的兜底。**不含 openid 尾巴**：那串编号会顺着 prompt 流进回复里，
# 人看不懂（见 _render_mention 的注释）。
MENTION_FALLBACK = "群友"

# 占位符类型 -> 渲染函数。渲染函数返回 None 或空串表示「这段没有可读信息」。
RENDERERS = {}


def renderer(*kinds):
    """把函数注册为某几种占位符的渲染器。"""
    def register(fn):
        for kind in kinds:
            RENDERERS[kind] = fn
        return fn
    return register


def normalize(text, mention_label=None):
    """把一条消息的 content 规范成「人能读的文本」。

    mention_label 是 callable(openid) -> str|None，用来把被 @ 的人翻成人话
    （上层拿关系档案实现）。不给也不影响使用，只是被 @ 的人退成「@群友」。

    返回空串表示这条消息没有可留存的内容（纯表情/纯图片/纯链接）。
    """
    if not text:
        return ""
    text = _MENTION.sub(lambda m: _render_mention(m.group(1), mention_label), text)
    text = _TAG.sub(_render_tag, text)
    text = _MEDIA_LINE.sub(" ", text)
    text = _URL.sub(" ", text)
    return _tidy(text)


# ── 内部实现 ──

def _render_mention(openid, mention_label):
    """@ 了谁。解析器认得出就用它的答案，认不出也留一个「这里有人」的痕迹。"""
    oid = (openid or "").strip()
    if not oid:
        return ""
    label = None
    if mention_label:
        try:
            label = mention_label(oid)
        except Exception:
            label = None       # 认人的表坏掉不该把整条消息带崩
    # 认不出是谁就只留「这里 @ 了个人」。
    # 以前这里会拼上 openid 的后四位（「@群友78D0」），本意是让没留名的人彼此区分，
    # 代价是这段机器编号会顺着会话历史和 prompt 一路流到模型嘴边 —— 它照着复读一句，
    # 群里看到的就是一串人类读不懂的乱码。区分人的活归关系档案，不靠这串编号。
    return "@" + (label or MENTION_FALLBACK)


def _render_tag(match):
    raw = match.group(1).strip()
    fn = RENDERERS.get(_kind(raw))
    # 认不出的占位符一律清掉：留着就是给模型看一串它读不懂的东西
    return fn(_attrs(raw)) if fn else ""


def _kind(raw):
    """占位符类型：<faceType=1,...> 取第一个键名。@ 型不走这里（见 _MENTION）。"""
    return raw.split("=", 1)[0].strip()


def _attrs(raw):
    """把 faceType=6,faceId="0",ext="xxx" 拆成 {"faceType": "6", "faceId": "0", ...}。"""
    return {m.group(1): (m.group(2) if m.group(2) is not None else m.group(3))
            for m in _ATTR.finditer(raw)}


def _ext_text(ext):
    """QQ 表情把配文藏在 ext 里：base64 的 {"text": "惊讶"}。解不出就是没有。"""
    if not ext:
        return ""
    try:
        blob = json.loads(base64.b64decode(ext + "=" * (-len(ext) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    return (blob.get("text") or "").strip() if isinstance(blob, dict) else ""


def _tidy(text):
    text = _WS.sub(" ", text)
    return _BLANK.sub("\n\n", text).strip()


# ── 各类型渲染器 ──

@renderer("faceType")
def _render_face(attrs):
    """表情。配文非空就是能读的表情；为空说明发的人没配字，不留痕迹。

    只认 ext 里的配文，不做 faceId 到表情名的对照表 —— 那是几百行的枚举，
    而 QQ 现在给的表情基本都是 faceId=0，真值全在 ext 里，查表也查不到。
    """
    text = _ext_text(attrs.get("ext", ""))
    return f"[表情：{text}]" if text else ""
