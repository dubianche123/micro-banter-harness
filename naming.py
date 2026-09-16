"""机器人自称 · 群主称呼 —— 两个「谁」的问题，集中一处回答。

为什么要单独有这个文件
----------------------
这两个名字会渗进提示词、彩蛋台词、日志和正则里，散着写就是硬编码：人设里写死
「你叫某某」、剥前缀的正则里写死「@某某」、黑名单里写死群主的外号 —— 换个人部署、
群主改个名，全得回来翻代码。这类机器人没法开源，也没法在第二个群里复用。

所以：**代码里一个具体人名都不留**，全部在这里解析。
  · 机器人叫什么 → BOT_NAMES（.env，**权威来源**，可配多个别名）+ 平台昵称（登录时
    GET /users/@me 读到的 QQ 用户名）。昵称只当**附带别名**：只会多一个叫法，
    顶不掉 BOT_NAMES 里配的那些；且只在启动时读一次，改了昵称要重启才生效。
  · 群主怎么称呼 → 他自己在关系档案里的自称（群里说一句「我是XX」就有），
    还没认领就退回通用词「群主」

模板里用 {bot} / {owner} 两个占位符，render() 负责替换。
"""

import config

# 兜底：既没配 BOT_NAMES、平台昵称也拿不到时用这个。刻意是个通用词而不是某个人名。
DEFAULT_BOT_NAME = "机器人"
DEFAULT_OWNER_LABEL = "群主"

# 平台（QQ）给出的机器人昵称，on_ready 时登记
_platform = {"name": ""}


def set_platform_name(name):
    _platform["name"] = (name or "").strip()


def platform_name():
    return _platform["name"]


def bot_names(platform=None):
    """机器人在群里的所有叫法。第一个当自称，全部用于「是不是在喊它」的识别与剥离。

    多个别名是真实需求：有人直接喊名字，有人喊「@名字」，指的都是它。
    """
    names = list(config.BOT_NAMES)
    pname = (_platform["name"] if platform is None else platform).strip()
    if pname and pname not in names:
        names.append(pname)
    return names or [DEFAULT_BOT_NAME]


def bot_name(platform=None):
    return bot_names(platform)[0]


def owner_label(nick=None):
    """群主怎么称呼。他认领过就用他的称呼，没认领就用通用词 —— 不凭空编。"""
    return (nick or "").strip() or DEFAULT_OWNER_LABEL


def render(text, bot=None, owner=None):
    """把模板里的 {bot} / {owner} 换成真名。

    刻意用 replace 而不是 str.format：提示词里有 {"aff": 2} 这种**字面花括号**
    （结构化输出协议），format 一碰就抛 KeyError，而且是运行到那一段才炸。
    """
    if not text:
        return text
    return (text.replace("{bot}", bot or bot_name())
                .replace("{owner}", owner or DEFAULT_OWNER_LABEL))
