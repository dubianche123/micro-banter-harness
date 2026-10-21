"""集中配置：凭证、AI 供应商、运行时参数。

凭证一律从环境变量读取（先加载同目录 .env），源码里不再出现任何密钥。
缺少必要凭证会直接启动失败并给出提示 —— 宁可 fail fast，也不要带残缺配置跑起来。
"""

import os
from pathlib import Path
from dotenv import load_dotenv as _load_dotenv
from regular.keys import read_api_keys

# CLI sets REGULAR_HOME explicitly; direct source runs keep the old project root.
BASE_DIR = str(Path(os.environ.get("REGULAR_HOME", Path(__file__).parent)).expanduser().resolve())
CONFIG_ERRORS = []


def load_dotenv(path=None):
    """Use python-dotenv's quoting/comments, without replacing real environment values."""
    return _load_dotenv(path or os.path.join(BASE_DIR, ".env"), override=False,
                        encoding="utf-8", interpolate=False)


load_dotenv()


def _env_str(name, default=""):
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default


def _env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    value = val.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    CONFIG_ERRORS.append(f"{name} 必须是 true/false")
    return default


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        CONFIG_ERRORS.append(f"{name} 数值无效")
        return default


def _env_float(name, default):
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        CONFIG_ERRORS.append(f"{name} 数值无效")
        return default


# ══════════════════ QQ 开放平台凭证 ══════════════════
QQ_APP_ID = _env_str("QQ_APP_ID")
QQ_APP_SECRET = _env_str("QQ_APP_SECRET")

# ══════════════════ 触发词 / 机器人身份 ══════════════════
# BOT_NAMES = 触发词：机器人在群里被叫到的名字（可多个别名，逗号分隔）。
# 它是部署时**最该先改的一处** —— 不配好，机器人在群里就是个哑巴：喊它不应、也不主动
# 接话，而且不报错（所以启动日志会把当前这串打出来，见 bot.on_ready）。
# **刻意不给默认人名**：代码里出现具体人名，换个人部署就得回来改代码，也没法开源。
# 留空就只认平台昵称（你在 QQ 那边定的机器人名字，进程一起来就知道），再不行才用通用词。
# 这一串同时管三件事：识别「是不是在叫它」、剥掉喊人前缀、提示词里的自称。
BOT_NAMES = [n.strip() for n in _env_str("BOT_NAMES", "").split(",") if n.strip()]

# ══════════════════ AI 供应商 ══════════════════
# All endpoints use the existing OpenAI-compatible SDK. Presets are conveniences,
# not statements about an account's model access, price, quota or reliability.
PROVIDER_PRESETS = {
    "openai": {
        "label": "OpenAI-compatible",
        "base_url": _env_str("OPENAI_BASE_URL"),
        "api_key": _env_str("OPENAI_API_KEY"),
        "models": [],
        "extra_body": {},
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": _env_str("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "api_key": _env_str("ZHIPU_API_KEY"),
        "models": ["glm-4.7", "glm-4.5-air", "glm-4-flash"],
        "models_digest": ["glm-4.5-air", "glm-4-flash"],
        "models_judge": ["glm-4.7"],
        "extra_body": {"thinking": {"type": "disabled"}},
        "temperature": 0.7,
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": _env_str("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        "api_key": _env_str("GEMINI_API_KEY"),
        "models": ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash"],
        "models_digest": ["gemini-3.5-flash-lite"],
        "models_judge": ["gemini-3.5-flash-lite"],
        "extra_body": {},
    },
}

for _provider_name, _preset in PROVIDER_PRESETS.items():
    for _tier_key in ("models", "models_digest", "models_judge"):
        _model_override = _env_str(f"{_provider_name.upper()}_{_tier_key.upper()}")
        if _model_override:
            _models = [name.strip() for name in _model_override.split(",") if name.strip()]
            if _models:
                _preset[_tier_key] = list(dict.fromkeys(_models))
            else:
                CONFIG_ERRORS.append(f"{_provider_name.upper()}_{_tier_key.upper()} 至少需要一个模型名")

AI_PROVIDER = _env_str("AI_PROVIDER", "openai").lower()
if AI_PROVIDER not in PROVIDER_PRESETS:
    CONFIG_ERRORS.append("AI_PROVIDER 必须是 openai、zhipu 或 gemini")
    AI_PROVIDER = "openai"
PROVIDER = PROVIDER_PRESETS[AI_PROVIDER]
FALLBACK_PROVIDER = _env_str("FALLBACK_PROVIDER", "").lower()
if FALLBACK_PROVIDER and FALLBACK_PROVIDER not in PROVIDER_PRESETS:
    CONFIG_ERRORS.append("FALLBACK_PROVIDER 必须为空、openai、zhipu 或 gemini")
    FALLBACK_PROVIDER = ""

def load_api_keys(provider=None):
    """多 Key 灾备轮询：同级目录建 api_keys_{provider}.txt，每行一个 Key（# 开头为注释）。

    按供应商分开存，避免把智谱的 Key 误喂给 Gemini。
    """
    provider = provider or AI_PROVIDER
    preset = PROVIDER_PRESETS[provider]
    key_file = os.path.join(BASE_DIR, f"api_keys_{provider}.txt")
    return read_api_keys(key_file, preset.get("api_key") or "")


AI_KEYS = load_api_keys()
CANDIDATE_MODELS = list(PROVIDER["models"])

# ══════════════════ 安全 / 网络 ══════════════════
# 默认验证 TLS。证书问题应修复证书链；兼容旧环境时可显式关闭，并由 doctor 提示。
SSL_VERIFY = _env_bool("SSL_VERIFY", True)

# ══════════════════ 运行数据路径 ══════════════════
# New installs keep runtime files together. Existing root-level deployments stay
# in place until the operator deliberately configures DATA_DIR; never move data on import.
_LEGACY_NAMES = ("state.json", "owner.txt", "archive", "memory")
LEGACY_DATA_PATHS = [os.path.join(BASE_DIR, n) for n in _LEGACY_NAMES
                     if os.path.exists(os.path.join(BASE_DIR, n))]
_explicit_data_dir = _env_str("DATA_DIR")
_data_location = Path(_explicit_data_dir or ("." if LEGACY_DATA_PATHS else "data")).expanduser()
DATA_DIR = str((Path(BASE_DIR) / _data_location).resolve())
LEGACY_DATA_LAYOUT = not _explicit_data_dir and bool(LEGACY_DATA_PATHS)


def data_path(name, default):
    """Explicit old per-file paths remain project-relative; defaults follow DATA_DIR."""
    value = _env_str(name)
    return str((Path(BASE_DIR) / Path(value).expanduser() if value else Path(DATA_DIR) / default).resolve())


LOG_FILE = data_path("LOG_FILE", "bot.log")
LOG_MAX_BYTES = _env_int("LOG_MAX_BYTES", 2 * 1024 * 1024)
LOG_BACKUP_COUNT = _env_int("LOG_BACKUP_COUNT", 5)
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
STATE_FILE = data_path("STATE_FILE", "state.json")
OWNER_FILE = data_path("OWNER_FILE", "owner.txt")
STATUS_FILE = data_path("STATUS_FILE", "status.json")

STATE_SAVE_INTERVAL = _env_float("STATE_SAVE_INTERVAL", 30.0)  # 脏数据刷盘间隔（秒）
SESSION_PRUNE_INTERVAL = _env_float("SESSION_PRUNE_INTERVAL", 600.0)

# ══════════════════ 会话上下文（滑动窗口）══════════════════
CONTEXT_MAX_TURNS = _env_int("CONTEXT_MAX_TURNS", 8)     # 最多记住多少个来回
CONTEXT_MAX_CHARS = _env_int("CONTEXT_MAX_CHARS", 3000)  # 上下文总字数上限，防止 token 爆炸
CONTEXT_TTL_SECONDS = _env_float("CONTEXT_TTL_SECONDS", 1800.0)  # 静默 30 分钟当作换话题

# ══════════════════ 群聊背景缓存 ══════════════════
GROUP_BUFFER_SIZE = _env_int("GROUP_BUFFER_SIZE", 10)
# 背景条目的保鲜期（秒）。⚠️ 只此一份：GROUP_BUFFER_TTL_SECONDS 是唯一在用的名字，
# 插嘴提示词「由头只从最近的话里挑」的口径就建立在它之上 —— 改这里会同时改变插嘴的眼界。

# ══════════════════ 限流（群级 + 全局，刻意不做 per-user 配额）══════════════════
# 令牌桶：每个群初始 N 个令牌，每 R 秒回补 1 个。既能扛住正常聊天的突发，
# 又能在有人连刷时把请求压到平均 ~60/R 次每分钟。
GROUP_RATE_CAPACITY = _env_float("GROUP_RATE_CAPACITY", 8.0)
GROUP_RATE_REFILL_SECONDS = _env_float("GROUP_RATE_REFILL_SECONDS", 5.0)
DAILY_BUDGET = _env_int("DAILY_BUDGET", 3000)  # 全局每日 AI 调用上限，兜底防账单爆炸
BUDGET_WARN_RATIO = _env_float("BUDGET_WARN_RATIO", 0.8)

# ══════════════════ AI 调用 ══════════════════
AI_TIMEOUT_SECONDS = _env_float("AI_TIMEOUT_SECONDS", 20.0)   # 单个模型

# 每次逻辑调用的总物理尝试数；候选来自显式模型/备用端点，不隐式重试。
AI_MAX_ATTEMPTS = _env_int("AI_MAX_ATTEMPTS", 3)

# 网关看门狗：botpy 收不到任何下行（含心跳 ACK）超过这么久，就主动断开 ws 触发重连。
# 心跳每 30s 一来一回，正常静默不会超过一分钟；阈值取 90s = 连续 3 个 ACK 没来。
# 为什么必须有它：botpy 的重连依赖「接收循环自己退出」，TCP 半死时 `receive()` 会永远挂住
# —— 实测 2026-09-19 13:15 一次 1006 之后整整 18 分钟没有任何重连动作，全靠这个补。
WS_WATCHDOG_STALE_SECONDS = _env_float("WS_WATCHDOG_STALE_SECONDS", 90.0)
AI_TOTAL_TIMEOUT = _env_float("AI_TOTAL_TIMEOUT", 30.0)       # 整个模型梯队
AI_MAX_TOKENS = _env_int("AI_MAX_TOKENS", 512)
AI_MAX_TOKENS_BANTER = _env_int("AI_MAX_TOKENS_BANTER", 120)
AI_TEMPERATURE = _env_float("AI_TEMPERATURE", 0.9)

# ══════════════════ 主动插嘴 / 彩蛋频率 ══════════════════
# 刻意不设文字长度门槛：群里一句「？」「太蠢了」就是真实的接话信号，拿字数当尺子
# 只会把能接的挡在外面（实测今天 257 条消息里，10 字门槛只放过 19 条，其中还有
# 三分之一是表情串）。频率交给概率 + 冷却控制。
# 实测（113 条候选 / 10.8 小时）8% ≈ 7 次/天，约每 1.5 小时一次。
# 2026-10-06 群主要求活跃气氛提到 12%（≈ 10~11 次/天，冷却 120s 仍然兜底）。
BANTER_COOLDOWN_SECONDS = _env_float("BANTER_COOLDOWN_SECONDS", 120.0)
BANTER_PROBABILITY = _env_float("BANTER_PROBABILITY", 0.12)

# 群里提到 / @ 群主时的插话概率。比普通插嘴高：群主是群里最现成的梗源，
# 群友拿他开涮的时候，机器人接一句最像真人（也最容易被人记住）。
# 「群主」的指代不写死具体人名 —— 通用词 + 群主自己认领的称呼 + 直接 @ 他，
# 三个来源在 bot.owner_reference_terms 里合起来判断，换个群、群主改个名都不用动代码。
OWNER_MENTION_PROBABILITY = _env_float("OWNER_MENTION_PROBABILITY", 0.30)

# 插嘴概率乘上「亲密度权重」之后的上限（relations.BANTER_WEIGHTS，熟 1.6× / 生 0.7×）。
# 权重只调「更愿意接谁」，不该把谁变成刷屏 —— 万一哪天把基数调大，天花板在这儿兜着。

# ── 主动性（2026-10-19，激活度系统的一部分，见 activation.py）──
# 心情/作息/群温的闸门全过时，自己照着最近的话题开个话头。所有闸门见 activation.proactive_ok：
# 4 小时间隔 / 群静默 10min~6h / 只在白天 / 心情不差。巡检每 30 分钟一次，纯检查零成本。
PROACTIVE_CHECK_SECONDS = _env_float("PROACTIVE_CHECK_SECONDS", 1800.0)

# ── 读图：默认复用主聊天模型，可为纯文本主模型单独指定视觉模型/供应商 ──
# 配置继承不代表模型具备图片能力；不支持图片时单配 VISION_MODEL，或关闭读图。
VISION_ENABLED = _env_bool("VISION_ENABLED", True)
VISION_PROVIDER = _env_str("VISION_PROVIDER", AI_PROVIDER).lower()
if VISION_PROVIDER not in PROVIDER_PRESETS:
    CONFIG_ERRORS.append("VISION_PROVIDER 必须是 openai、zhipu 或 gemini")
    VISION_PROVIDER = AI_PROVIDER
_vision_model = _env_str("VISION_MODEL")
VISION_MODEL_INHERITED = not _vision_model and VISION_PROVIDER == AI_PROVIDER
VISION_MODEL = _vision_model or (CANDIDATE_MODELS[0] if VISION_MODEL_INHERITED and CANDIDATE_MODELS else "")
VISION_INPUT_MODE = _env_str("VISION_INPUT_MODE", "url").lower()
if VISION_INPUT_MODE not in ("url", "download"):
    CONFIG_ERRORS.append("VISION_INPUT_MODE 必须是 url 或 download")
VISION_COOLDOWN_SECONDS = _env_float("VISION_COOLDOWN_SECONDS", 60.0)
VISION_DAILY_CAP = _env_int("VISION_DAILY_CAP", 30)
BANTER_CHANCE_MAX = _env_float("BANTER_CHANCE_MAX", 0.5)

MEME_COOLDOWN_SECONDS = _env_float("MEME_COOLDOWN_SECONDS", 60.0)
MEME_PROBABILITY = _env_float("MEME_PROBABILITY", 0.40)

# ══════════════════ 群友关系档案（见 relations.py）══════════════════
AFFINITY_ENABLED = _env_bool("AFFINITY_ENABLED", True)
# 每轮是否让模型在回复末尾吐一行 <CMD>{"aff":n}</CMD>。
#
# 2026-10-06 改过语义：以前是让它给 -3~+3 的**自由数字**，实测很虚（普通聊天也给 +3），
# 而且没有任何东西能拦住它。现在只让它判**方向**（-1/0/1）——「这句对我是什么态度」是它的
# 强项，绝对幅度交给代码：命中本地词表就扣，方向对上就加，最后还要过每日封顶。
# 关掉（默认）后好感度只由每日压缩统一评估。
CMD_PROTOCOL_ENABLED = _env_bool("CMD_PROTOCOL_ENABLED", True)
# 每日加分/扣分封顶（对称）。为什么需要：模型偶尔会判错，而好感度是**长期账本** ——
# 没有封顶的话，一次判错就可能把「铁哥们」直接扣成「生疏」，且要靠很多天才能涨回来。
AFFINITY_DAILY_GAIN_CAP = _env_int("AFFINITY_DAILY_GAIN_CAP", 10)
AFFINITY_DAILY_LOSS_CAP = _env_int("AFFINITY_DAILY_LOSS_CAP", 5)
# 压缩时一次性结算一整天的印象变化，幅度上限比单次互动（±3）放宽些
AFFINITY_DIGEST_SPAN = _env_int("AFFINITY_DIGEST_SPAN", 6)
AFFINITY_DECAY_GRACE_DAYS = _env_float("AFFINITY_DECAY_GRACE_DAYS", 3.0)  # 这几天不来往不扣
AFFINITY_DECAY_STEP_DAYS = _env_float("AFFINITY_DECAY_STEP_DAYS", 2.0)    # 之后每 N 天向 0 收敛一次
AFFINITY_DECAY_AMOUNT = _env_int("AFFINITY_DECAY_AMOUNT", 1)
AFFINITY_PRUNE_IDLE_DAYS = _env_float("AFFINITY_PRUNE_IDLE_DAYS", 90.0)   # 长期不互动就清掉档案
AFFINITY_BOARD_SIZE = _env_int("AFFINITY_BOARD_SIZE", 5)                  # 排行榜显示人数
# 切换人格模式（猫娘/风纪/发疯…）的最低交情分 —— 见 bot.mode_switch_allowed。
# 事故：2026-09-21 群里家豪好感度 −6（一直骂人）照样一句「猫娘模式」就切换成功，太没分寸。
# 默认 0 = familiar 档起步：打过交道但印象一般/更差的会被回绝；**没打过交道的新人放行**
# （不能因为还不熟就被拒，那是把门槛用成排斥）。群主永远豁免。
MODE_SWITCH_MIN_AFFINITY = _env_int("MODE_SWITCH_MIN_AFFINITY", 0)

# ── 心情闸门（2026-10-22 群主：他说他很烦，一句猫娘模式就进去了，违和）──
# 元气/玩闹类模式在心情差时演不了：低于门槛的切换被回绝（台词符合心情，不提内部机制）。
# 消极类（crazy 发疯）不设门槛 —— 烦的时候发疯反而最真，只有「烦+猫娘」才违和。
MODE_MOOD_FLOOR = _env_float("MODE_MOOD_FLOOR", -0.5)

# ── 自动发疯（给懒得喊口令的群友的自动化路径）──
# 心情 ≤ AUTO_CRAZY_THRESHOLD 持续 AUTO_CRAZY_ENTER_MINUTES 分钟 → 自动进 crazy；
# 回升到 AUTO_CRAZY_EXIT 之上 → 自动退。只对**自动进的** crazy 生效，手动进的归手动管。
# 每天自动进入 AUTO_MODE_DAILY_CAP 次封顶（防抖：心情反复横跳也不能一天发疯八回）。
AUTO_CRAZY_THRESHOLD = _env_float("AUTO_CRAZY_THRESHOLD", -0.7)
AUTO_CRAZY_ENTER_MINUTES = _env_float("AUTO_CRAZY_ENTER_MINUTES", 30.0)
AUTO_CRAZY_EXIT = _env_float("AUTO_CRAZY_EXIT", -0.2)
AUTO_MODE_DAILY_CAP = _env_int("AUTO_MODE_DAILY_CAP", 2)

# ══════════════════ 群聊长期记忆（每日滚动摘要，见 digest.py）══════════════════
DIGEST_ENABLED = _env_bool("DIGEST_ENABLED", True)
DIGEST_INTERVAL_HOURS = _env_float("DIGEST_INTERVAL_HOURS", 24.0)  # 多久压一次
DIGEST_MIN_PENDING = _env_int("DIGEST_MIN_PENDING", 15)            # 消息太少不值得压
# 攒够这么多条就压一次，不必干等 24 小时 —— 活跃的群当天就能刷新长期记忆与好感度。
# DIGEST_INTERVAL_HOURS 退化成兜底：冷清的群也至少一天压一次。
DIGEST_COUNT_TRIGGER = _env_int("DIGEST_COUNT_TRIGGER", 120)
DIGEST_CHUNK_CHARS = _env_int("DIGEST_CHUNK_CHARS", 6000)          # 单块字符上限
DIGEST_MAX_BLOCKS = _env_int("DIGEST_MAX_BLOCKS", 10)              # 一次最多分几块
DIGEST_MAX_PENDING = _env_int("DIGEST_MAX_PENDING", 2000)          # 单次最多喂给模型多少条
DIGEST_CHECK_INTERVAL = _env_float("DIGEST_CHECK_INTERVAL", 1800.0)  # 后台检查间隔（秒）
DIGEST_MAX_TOKENS = _env_int("DIGEST_MAX_TOKENS", 800)
# 人物名册里一条「近况」能喂给模型多少天（见 digest.stamp_people / render_people）。
# 名册的定位本来就是「最近在干什么」，不是永久档案；模型每次压缩又习惯把旧条目原样抄回，
# 所以靠时间戳让它自然过期 —— 实测「号刚解封」这条标签挂了五天，天天被拿来说。
PEOPLE_NOTE_MAX_AGE_DAYS = _env_float("PEOPLE_NOTE_MAX_AGE_DAYS", 2.0)
# 「名场面/梗」同样有时效（见 digest.stamp_memes / render_summary）。
# 原先只喂列表里第一条、且不限时，于是排到首位的那条梗天天在模型眼前 ——
# 收录进去的偏偏是「你死了十二天」这种针对它自己的吐槽。
MEME_NOTE_MAX_AGE_DAYS = _env_float("MEME_NOTE_MAX_AGE_DAYS", 3.0)
# 群聊背景（【群聊最近背景】）最多回看多久。buffer 会落盘、跨重启保留，
# 不用时间过滤的话，安静半天后的第一条消息配到的还是半天前的话。
GROUP_BUFFER_TTL_SECONDS = _env_float("GROUP_BUFFER_TTL_SECONDS", 1800.0)
# QQ 官方 API 的 HTTP 超时（秒）。botpy 默认 5s，晚间高峰实测会超 —— 而且 botpy 的
# http 层把 TimeoutError 吞掉不抛（只打警告返回 None），超时=这条回复凭空消失。
# 放宽到 10s 减少发生频率；真发生了由 safe_reply 的「None 检测 + 同 seq 重试」兜底。
QQ_API_TIMEOUT = _env_float("QQ_API_TIMEOUT", 10.0)
# 摘要产出后顺手写一份 MD，人直接看这个；原始消息另存 JSONL 备查
DIGEST_WRITE_MD = _env_bool("DIGEST_WRITE_MD", True)

# ══════════════════ 原始消息归档（见 archive.py）═════════════════
# 摘要是有损压缩，出错时得有地方回查，所以原文单独按天留存。
ARCHIVE_DIR = data_path("ARCHIVE_DIR", "archive")
MEMORY_DIR = data_path("MEMORY_DIR", "memory")
ARCHIVE_KEEP_DAYS = _env_int("ARCHIVE_KEEP_DAYS", 90)   # 原始消息保留天数，0 = 永不清理
ARCHIVE_MAX_TEXT = _env_int("ARCHIVE_MAX_TEXT", 500)    # 单条消息存多少字

# ══════════════════ 承诺催债（见 digest.PromiseBook）═════════════════
PROMISE_ENABLED = _env_bool("PROMISE_ENABLED", True)
PROMISE_GRACE_HOURS = _env_float("PROMISE_GRACE_HOURS", 24.0)      # 立下之后多久才允许催
PROMISE_NAG_INTERVAL_HOURS = _env_float("PROMISE_NAG_INTERVAL_HOURS", 48.0)  # 两次催债间隔
PROMISE_MAX_NAG = _env_int("PROMISE_MAX_NAG", 2)                   # 催满几次就撤下，别变骚扰
PROMISE_MAX_ITEMS = _env_int("PROMISE_MAX_ITEMS", 12)              # 每群最多记几条
PROMISE_CHECK_INTERVAL = _env_float("PROMISE_CHECK_INTERVAL", 3600.0)  # 后台检查间隔（秒）
# 这些小时段内不主动开口（本地时间），免得半夜把群吵醒
def _quiet_hours():
    try:
        hours = {int(h.strip()) for h in _env_str("PROMISE_QUIET_HOURS", "23,0,1,2,3,4,5,6,7,8").split(",") if h.strip()}
        if not hours.issubset(range(24)):
            raise ValueError
        return hours
    except ValueError:
        CONFIG_ERRORS.append("PROMISE_QUIET_HOURS 必须是逗号分隔的 0~23 小时")
        return set()


PROMISE_QUIET_HOURS = _quiet_hours()

# ══════════════════ 其它 ══════════════════
DEDUP_MAX = _env_int("DEDUP_MAX", 500)   # 消息去重 ID 队列长度
RECONNECT_MIN_DELAY = _env_float("RECONNECT_MIN_DELAY", 5.0)
RECONNECT_MAX_DELAY = _env_float("RECONNECT_MAX_DELAY", 300.0)
RESET_BACKOFF_AFTER = _env_float("RESET_BACKOFF_AFTER", 120.0)  # 稳定运行这么久就重置退避计数


def validate():
    """启动前校验，缺东西就 fail fast。"""
    errors = list(CONFIG_ERRORS)
    for name in ("BANTER_PROBABILITY", "OWNER_MENTION_PROBABILITY", "BANTER_CHANCE_MAX", "MEME_PROBABILITY", "BUDGET_WARN_RATIO"):
        if not 0 <= globals()[name] <= 1:
            errors.append(f"{name} 必须在 0~1 之间")
    for name in ("DAILY_BUDGET", "VISION_DAILY_CAP"):
        if globals()[name] < 0:
            errors.append(f"{name} 不能为负数")
    if not QQ_APP_ID:
        errors.append("缺少 QQ_APP_ID（在 .env 里配置）")
    if not QQ_APP_SECRET:
        errors.append("缺少 QQ_APP_SECRET（在 .env 里配置）")
    if VISION_ENABLED and not VISION_MODEL:
        if VISION_PROVIDER != AI_PROVIDER:
            errors.append("VISION_PROVIDER 与 AI_PROVIDER 不同；请显式设置该供应商的 VISION_MODEL")
        else:
            errors.append("读图无法继承主聊天模型；请配置主供应商的 MODELS 或单独设置 VISION_MODEL")
    if AI_MAX_ATTEMPTS < 1:
        errors.append("AI_MAX_ATTEMPTS 必须至少为 1")
    if AI_TIMEOUT_SECONDS <= 0 or AI_TOTAL_TIMEOUT <= 0:
        errors.append("AI_TIMEOUT_SECONDS 和 AI_TOTAL_TIMEOUT 必须大于 0")
    for provider_name in dict.fromkeys((AI_PROVIDER, FALLBACK_PROVIDER)):
        if not provider_name:
            continue
        preset = PROVIDER_PRESETS[provider_name]
        if not preset.get("base_url"):
            errors.append(f"缺少 {provider_name.upper()}_BASE_URL")
        if not preset["models"]:
            errors.append(f"缺少 {provider_name.upper()}_MODELS")
    if not AI_KEYS:
        errors.append(f"缺少 {PROVIDER['label']} 的 API Key（在 .env 或 api_keys_{AI_PROVIDER}.txt 里配置）")
    return errors
