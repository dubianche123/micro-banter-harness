"""敏感词过滤 —— 一处维护，多处复用。

为什么单独有这个文件
--------------------
群里总有人专挑「怎么说都不该说的词」来试机器人：政治人名及其谐音、地域黑、拿身份开涮。
这些词有两条完全不同的泄漏路径，各修各的没用：

  1. **入口**：群友把某个词认领成称呼，机器人从此天天这么叫（起名接口）
  2. **回流**：模型压缩记忆时照抄原话，把词固化进长期记忆，之后每一轮对话都带着它（摘要接口）

两处都用得上同一张词表，所以抽到这里，别再各写一份。

实测结论：这两处**都不能只靠提示词**（16 次对照调用，含同一份真实聊天记录的前后对比）——
给摘要提示词加上「敏感内容不复述」的硬规则之后，输出里命中的词数**一处没少**（2 处 vs 2 处）。
模型压根不认为那是个敏感词，它当成普通昵称照抄了。谐音梗本来就是设计来绕过识别的。

所以底线放在这里：一张命中即拦的词表。提示词该写的还是写（成本为零，偶尔能多挡一层），
但**不能指望它**。

词表分两层
----------
- 内置层（BUILTIN_WORDS）：通用词，跟着代码走。只放**词形层面**的判据 ——
  职务/尊号是谐音梗的重灾区，长辈/主子称谓是骚扰与钓鱼的常见开场。这里不放任何具体人名：
  人名事件随语境变化，硬编码进仓库既维护不动，也等于把词固化进了代码历史。
- 外部层（sensitive_nicks.txt）：具体词，由使用者自己维护，一行一个，按 mtime 热重载，
  加词不用重启进程。
"""

import os
import re
import threading

# ══════════════════════════ 内置通用词 ══════════════════════════

# 职务/尊号：谐音梗的重灾区，拿它当外号被人截图必然出事
_TITLE_WORDS = (
    "主席", "总书记", "总理", "委员长", "总统", "首相", "书记", "部长", "省长",
    "市长", "县长", "局长", "陛下", "殿下", "圣上", "皇上", "皇帝", "教主",
)

# 身份僭越：把机器人或群友架到长辈/主子位上，是骚扰与钓鱼最常见的开场
_ARROGANT_WORDS = (
    "爸爸", "父亲", "爹", "爷爷", "祖宗", "主人", "老爷", "大爷",
)

BUILTIN_WORDS = _TITLE_WORDS + _ARROGANT_WORDS

# ══════════════════════════ 外部词表 ══════════════════════════

DENY_FILE = "sensitive_nicks.txt"

_lock = threading.Lock()
_cache = {"mtime": None, "path": None, "words": ()}


def external_words():
    """读外部词表。一行一个词，# 开头是注释；文件不存在就当空表。

    带 mtime 检查：改完文件立即生效，不用重启机器人（起名/压缩都是低频路径，
    这点 stat 开销可以忽略）。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DENY_FILE)
    with _lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            _cache.update({"mtime": None, "path": path, "words": ()})
            return ()
        if _cache["mtime"] != mtime or _cache["path"] != path:
            words = []
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            words.append(line)
            except OSError:
                words = []
            _cache.update({"mtime": mtime, "path": path, "words": tuple(words)})
        return _cache["words"]


# 运行时注入的词：**不落盘、不进仓库**。目前唯一的用处是「认主暗号」——
# 暗号要是被人在群里念出来，它就可能顺着摘要回流进长期记忆，之后每一轮 prompt 都带着它，
# 等于把口令抄进了模型上下文。注册进来之后，摘要出口会连同其他敏感词一起把它抹掉。
# 只在 2 字以上才收：一个字的口令本来就该改掉，而且单字误伤面太大。
_runtime = set()


def add_runtime_words(words):
    """登记一批只存在于内存里的敏感词。返回实际收下的条数。"""
    added = 0
    for w in (words or ()):
        w = (w or "").strip()
        if len(w) >= 2 and w not in _runtime:
            _runtime.add(w)
            added += 1
    return added


def all_words():
    """内置 + 外部 + 运行时注入，去重后按长度倒序（长的先替换，避免短词把长词切碎）。"""
    words = set(BUILTIN_WORDS) | set(external_words()) | set(_runtime)
    return tuple(sorted(words, key=len, reverse=True))


def hits(text):
    """命中了哪些词（去重）。空文本返回空元组。"""
    if not text:
        return ()
    return tuple(w for w in all_words() if w in text)


def scrub(text, placeholder="【已隐去】"):
    """把命中的词就地替换成占位符。

    用在「模型可能照抄」的产出上（摘要正文、结构化字段）。
    刻意做替换而不是删行：删行会把句子截断成半截，看着像坏了；
    就地替换既保住了句子的可读性，也把词剥干净了。
    """
    if not text:
        return text
    for w in all_words():
        if w in text:
            text = text.replace(w, placeholder)
    return text


def has_hit(text):
    return bool(hits(text))


# ══════════════════════════ 称呼形态校验 ══════════════════════════

# 名字不该带这些东西：@ 占位符、换行、尖括号
RE_BAD_CHARS = re.compile(r"[<@>\n\r]")

# 称呼长度上限：长了基本是句子不是名字
MAX_NICK_LEN = 12
