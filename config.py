"""集中配置：凭证、AI 供应商、运行时参数。

凭证一律从环境变量读取（先加载同目录 .env），源码里不再出现任何密钥。
缺少必要凭证会直接启动失败并给出提示 —— 宁可 fail fast，也不要带残缺配置跑起来。
"""

import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ── 简易 .env 加载器（不引入额外依赖；真实环境变量优先级高于 .env）──
def load_dotenv(path=None):
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        return True
    except Exception as e:
        print(f"⚠️ 读取 .env 失败: {e}")
        return False


load_dotenv()


def _env_str(name, default=""):
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default


def _env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
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

# 认领群主用的暗号，**只在私聊里认**（群里说破天也不授权，见 bot.handle_group_msg）。
#
# 为什么必须私聊：群聊是大声公。认领动作一旦放在群里，口令就等于念给所有人听，
# 而且是谁先喊谁得 —— 随手一个群成员，甚至不在群里的人，都能把身份抢走。
# 私聊只有当事人自己和机器人看得到，抢答这条路直接没了。
#
# 又为什么是「可配置」而不是写死一句：写死的口令等于公开的口令。默认值只是让开箱
# 能用，真正部署时应该改成只有你知道的一句 —— `.env` 里覆盖即可，改完重启生效。
# 留空 = 关闭私聊认主通道（那台机器只能靠手动写 owner.txt 认领）。
DEFAULT_OWNER_CLAIM_PHRASE = "认主"
OWNER_CLAIM_PHRASE = _env_str("OWNER_CLAIM_PHRASE", DEFAULT_OWNER_CLAIM_PHRASE)

# ══════════════════ AI 供应商 ══════════════════
# 两家都走 OpenAI 兼容协议，所以调用代码完全共用，切换只需改 AI_PROVIDER。
PROVIDER_PRESETS = {
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": _env_str("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "api_key": _env_str("ZHIPU_API_KEY"),
        # 正经对话走这个梯队：4.7 质量明显更好但慢 3~6 倍，额度耗尽(429)自动回落 4.5-air，
        # 再不行上免费的 4-flash。三档都实测可用。
        "models": ["glm-4.7", "glm-4.5-air", "glm-4-flash"],
        # 压缩/后台批处理走这个。分的是「任务」，不是「快慢」—— 换模型要挑那个换了不亏的地方：
        #   ① 压缩的 prompt（SYSTEM_MAP/SYSTEM_REDUCE）跟聊天**不共享任何前缀**，换模型零缓存损失；
        #   ② 压缩在后台跑，不怕慢；
        #   ③ 压缩不参与人格，弱模型不会让机器人「插嘴时突然变笨」；
        #   ④ 实测更强的那档**反而更容易被内容过滤拒掉** —— 同一份今日 transcript，4.7 直接
        #      1301 contentFilter，4.5-air 正常出结果。压缩要吞原始聊天，这活不适合强模型。
        # 刻意不追加主梯队：两个都挂了就等下一轮，消息留在归档里不会丢（last_run 不推进）。
        "models_digest": ["glm-4.5-air", "glm-4-flash"],
        # 称呼审核梯队：这活要的是「认得出来」，只有最强那档干得了 ——
        # 实测同一批用例：4.5-air 把谐音称呼放行、还把正常昵称「阿澈」误杀；4.7 七个全对。
        # 起名是低频操作（有人起名才触发），单次约 137 token；整条链挂掉时调用方直接放行，
        # 不因为审核不了就阻断起名（本地词表仍在兜底）。
        "models_judge": ["glm-4.7"],
        # ⚠️ 别迷信 GET /models —— 它只列了 10 个付费模型，glm-4-flash / 4.5-flash / 4.7-flash
        #    都不在列表里却实际能调。列表里没写不等于不能用，反过来也不等于有额度。
        # ⚠️ 别加 glm-4.6 / glm-5 / glm-5-turbo / glm-5.3-flash：这个 Key 对它们全是「余额不足」。
        # ⚠️ 千万别再写第二个 "models" 键 —— Python 取后一个，会把上面的梯队整条顶掉（踩过）。
        # glm-4.5 系列默认先「思考」再作答（答案之外还有个 reasoning_content）。群聊秒回必须关掉，
        # 否则既慢（实测 3.1s vs 0.5s），又容易因思考 token 占满 max_tokens 导致最终答案 content 为空。
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": _env_str("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        "api_key": _env_str("GEMINI_API_KEY"),
        # 免费层里 3.5-flash-lite 有 500 次/天配额，其余仅 20 次/天，所以必须排第一个当工作马。
        # 它是这一档里**又快又不笨**的那个：实测带 1186 字 system 的插嘴请求 1.1~1.7s
        # （同请求 4.7 要 4.7s），且 token 计数只有智谱的三分之一（297 vs 1149）。
        # 不配 3.8-flash：实测 5.5s 才吐 1 个字，慢且没必要。
        "models": ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash"],
        # 称呼审核梯队。**不挑最强，挑最准** —— 实测同一批用例（2 个敏感称呼 + 6 个正常昵称）：
        #   gemini-3.5-flash-lite  8/8 全对
        #   gemini-3.5-flash       误杀「阿澈」「阿龙」两个正常昵称
        # 「更强 = 判得更准」在这里不成立：高档模型倾向于把陌生专有名词一律当风险。
        "models_judge": ["gemini-3.5-flash-lite"],
        # 压缩梯队。实测它能吞下 260 条原始聊天（4732 字）并正常产出。
        # 选它还有个附加好处：同一份含政治人名的 transcript，4.5-air 会把「评价某某某」照抄进摘要，
        # 而它主动写成「有人在群里试探敏感话题（不复述）」—— 摘要每轮都会注入 prompt，
        # 少一句复述就少一处固化源。
        "models_digest": ["gemini-3.5-flash-lite"],
        # 单模型超时。这条路要经代理出国，链路不稳时**卡住**比**报错**更常见：
        # 实测正常 1.1~1.7s，给 12s 已经很宽裕，超了就不值得再等 —— 留给智谱答。
        "timeout": 12.0,
        "extra_body": {},
    },
}

AI_PROVIDER = _env_str("AI_PROVIDER", "zhipu").lower()
if AI_PROVIDER not in PROVIDER_PRESETS:
    print(f"⚠️ 未知的 AI_PROVIDER={AI_PROVIDER!r}，已回退到智谱 zhipu")
    AI_PROVIDER = "zhipu"
PROVIDER = PROVIDER_PRESETS[AI_PROVIDER]

# 备用供应商：主供应商整条链路不通时自动回落（典型场景是代理断了，Gemini 连不上）。
# 留空或设为与 AI_PROVIDER 相同 = 不降级。降级只在模型调用层生效，与 /models 探活无关。
FALLBACK_PROVIDER = _env_str("FALLBACK_PROVIDER", "zhipu").lower()
if FALLBACK_PROVIDER and FALLBACK_PROVIDER not in PROVIDER_PRESETS:
    print(f"⚠️ 未知的 FALLBACK_PROVIDER={FALLBACK_PROVIDER!r}，已禁用降级")
    FALLBACK_PROVIDER = ""
# 连续失败这么多次就把这家暂时拉黑，免得每条消息都干等一个超时周期。
# 阈值定 2（原来 3）：主供应商是走代理的 Gemini，链路抖一下就该让位给直连的智谱，
# 没必要连撞三次。连接层/地区类错误不数次数，直接按 hard 处理（见 bot._note_provider_fail）。
PROVIDER_FAIL_THRESHOLD = _env_int("PROVIDER_FAIL_THRESHOLD", 2)
# 冷却从 300 降到 120：主供应商不稳，冷却期太长会让「优先 Gemini」名存实亡 ——
# 网络抖动通常几十秒就恢复，而探测成本很低（连接失败实测只要 1.3~2.3s），
# 所以宁可让它早点回来重试，也不要一挂就晾五分钟。
# ⚠️ 这只是**最短**让位时间，不是唯一条件 —— 能不能回来还要看下面那条缓存窗口。
PROVIDER_COOLDOWN_SECONDS = _env_float("PROVIDER_COOLDOWN_SECONDS", 120.0)

# 结构性故障（地区封禁 / Key 失效）的隔离时长，默认 30 分钟。
# 这一类跟上面的「抖动」不是一回事：Gemini 的 `User location is not supported`
# 由出口 IP 决定，两分钟后再试必然还是同一个错。实测 2026-09-17~18 一天半里
# 这类 400 撞了 **39 次** —— 每两分钟被放回来重撞一轮，每轮还会顺着模型梯队连试 3 档，
# 全员熔断时更要被当成「冷却剩余最短」的那家挑去带伤上阵。
# 30 分钟的依据：够长到不再刷无效请求，也够短到换代理或换 Key 之后能在可接受时间内自愈
# （真要立刻恢复，改完重启进程即可）。
PROVIDER_FATAL_COOLDOWN_SECONDS = _env_float("PROVIDER_FATAL_COOLDOWN_SECONDS", 1800.0)

# 缓存还热着的窗口：群里距最后一条消息不超过这么久，就认为被让位那家的前缀缓存
# 还没凉，**先别切回去**（切换要重新 prefill 一整段，等于把缓存白扔）。
# 等群静到超过这个时长，缓存该过期了，这时候回去重试才是免费的。
#
# 定标依据是实测，不是官方文档 —— 官方只说「缓存有合理的时效性」：
#   · 智谱（tests/probe_cache_ttl.py，1340 字前缀 ≈ 994 token）：复用命中 99%，
#     距上次请求 60s / 120s / 240s / **420s 仍命中**，边界没探到 → 取 600s 留余量。
#   · Gemini：同一次实测里 cached_tokens 从头到尾 **都是 0**，连「立刻复用」那次也是。
#     注意请求体实测 1088~1093 token，**已经超过官方 1024 的下限** —— 所以不是「不够大」，
#     就是这一档模型在我们这个体积下吃不到隐式缓存。
#     推论：「缓存优先」实际上只在智谱这一侧有意义，切回 Gemini 不损失任何缓存。
# 群一直热聊就一直留在备用供应商上 —— 这是**有意**的取舍：缓存优先于速度。
# 代价是这段时间回复慢（4.7 实测 ~4.7s vs 3.5-flash-lite ~1.3s），嫌慢就往下调小。
# 例外：备用供应商也服务不了时必须让主供应商顶上，否则整条链全哑（见 bot._provider_available）。
PROVIDER_CACHE_WARM_SECONDS = _env_float("PROVIDER_CACHE_WARM_SECONDS", 600.0)


def load_api_keys(provider=None):
    """多 Key 灾备轮询：同级目录建 api_keys_{provider}.txt，每行一个 Key（# 开头为注释）。

    按供应商分开存，避免把智谱的 Key 误喂给 Gemini。
    """
    provider = provider or AI_PROVIDER
    preset = PROVIDER_PRESETS[provider]
    key_file = os.path.join(BASE_DIR, f"api_keys_{provider}.txt")
    default_key = preset.get("api_key") or ""
    if not os.path.exists(key_file):
        return [default_key] if default_key else []
    try:
        with open(key_file, encoding="utf-8") as f:
            keys = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except Exception as e:
        print(f"⚠️ 读取 {os.path.basename(key_file)} 失败: {e}")
        return [default_key] if default_key else []
    if keys:
        print(f"🔑 已从 {os.path.basename(key_file)} 加载 {len(keys)} 个 {preset['label']} Key")
    return keys or ([default_key] if default_key else [])


AI_KEYS = load_api_keys()
CANDIDATE_MODELS = list(PROVIDER["models"])

# ══════════════════ 安全 / 网络 ══════════════════
# 默认仍关闭 SSL 校验（与改造前行为一致，规避本机证书链问题）。
# 若本机证书链正常，强烈建议 SSL_VERIFY=true —— 关闭校验意味着存在中间人攻击面。
SSL_VERIFY = _env_bool("SSL_VERIFY", False)

# ══════════════════ 日志 ══════════════════
LOG_FILE = os.path.join(BASE_DIR, _env_str("LOG_FILE", "bot.log"))
LOG_MAX_BYTES = _env_int("LOG_MAX_BYTES", 2 * 1024 * 1024)   # 超过就轮转
LOG_BACKUP_COUNT = _env_int("LOG_BACKUP_COUNT", 5)
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()

# ══════════════════ 持久化 ══════════════════
STATE_FILE = os.path.join(BASE_DIR, _env_str("STATE_FILE", "state.json"))
OWNER_FILE = os.path.join(BASE_DIR, _env_str("OWNER_FILE", "owner.txt"))
STATE_SAVE_INTERVAL = _env_float("STATE_SAVE_INTERVAL", 30.0)  # 脏数据刷盘间隔（秒）
SESSION_PRUNE_INTERVAL = _env_float("SESSION_PRUNE_INTERVAL", 600.0)

# ══════════════════ 会话上下文（滑动窗口）══════════════════
CONTEXT_MAX_TURNS = _env_int("CONTEXT_MAX_TURNS", 8)     # 最多记住多少个来回
CONTEXT_MAX_CHARS = _env_int("CONTEXT_MAX_CHARS", 3000)  # 上下文总字数上限，防止 token 爆炸
CONTEXT_TTL_SECONDS = _env_float("CONTEXT_TTL_SECONDS", 1800.0)  # 静默 30 分钟当作换话题

# ══════════════════ 群聊背景缓存 ══════════════════
GROUP_BUFFER_SIZE = _env_int("GROUP_BUFFER_SIZE", 10)
GROUP_BUFFER_TTL = _env_float("GROUP_BUFFER_TTL", 1800.0)

# ══════════════════ 限流（群级 + 全局，刻意不做 per-user 配额）══════════════════
# 令牌桶：每个群初始 N 个令牌，每 R 秒回补 1 个。既能扛住正常聊天的突发，
# 又能在有人连刷时把请求压到平均 ~60/R 次每分钟。
GROUP_RATE_CAPACITY = _env_float("GROUP_RATE_CAPACITY", 8.0)
GROUP_RATE_REFILL_SECONDS = _env_float("GROUP_RATE_REFILL_SECONDS", 5.0)
DAILY_BUDGET = _env_int("DAILY_BUDGET", 3000)  # 全局每日 AI 调用上限，兜底防账单爆炸
BUDGET_WARN_RATIO = _env_float("BUDGET_WARN_RATIO", 0.8)

# ══════════════════ AI 调用 ══════════════════
AI_TIMEOUT_SECONDS = _env_float("AI_TIMEOUT_SECONDS", 20.0)   # 单个模型

# 单个模型超时后，这一档在多长时间内先跳过（默认 10 分钟）。
# 理由见 bot._penalize_slow_model：梯队顺序是共享引用，「往下挪一位」就再也回不去，
# 而晚高峰变慢的模型过一阵往往又快又好；所以记的是**到期时间**而不是新顺序，
# 凉够了自己回到原位试运行 —— 答上来就留用，再超时就再罚一轮。
# ⚠️ 不能省：每一轮都先撞一次队首的慢档，剩下的预算常常连第二档都跑不完
# （实测 2026-09-18 晚，glm-4.7 一小时超时 4 次，每次都把整轮拖成就地兜底话术）。
MODEL_SLOW_PENALTY_SECONDS = _env_float("MODEL_SLOW_PENALTY_SECONDS", 600.0)

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
# 实测（113 条候选 / 10.8 小时）8% ≈ 7 次/天，约每 1.5 小时一次；嫌多往下调即可。
BANTER_COOLDOWN_SECONDS = _env_float("BANTER_COOLDOWN_SECONDS", 120.0)
BANTER_PROBABILITY = _env_float("BANTER_PROBABILITY", 0.08)

# 群里提到 / @ 群主时的插话概率。比普通插嘴高：群主是群里最现成的梗源，
# 群友拿他开涮的时候，机器人接一句最像真人（也最容易被人记住）。
# 「群主」的指代不写死具体人名 —— 通用词 + 群主自己认领的称呼 + 直接 @ 他，
# 三个来源在 bot.owner_reference_terms 里合起来判断，换个群、群主改个名都不用动代码。
OWNER_MENTION_PROBABILITY = _env_float("OWNER_MENTION_PROBABILITY", 0.25)

# 插嘴概率乘上「亲密度权重」之后的上限（relations.BANTER_WEIGHTS，熟 1.6× / 生 0.7×）。
# 权重只调「更愿意接谁」，不该把谁变成刷屏 —— 万一哪天把基数调大，天花板在这儿兜着。
BANTER_CHANCE_MAX = _env_float("BANTER_CHANCE_MAX", 0.5)

MEME_COOLDOWN_SECONDS = _env_float("MEME_COOLDOWN_SECONDS", 60.0)
MEME_PROBABILITY = _env_float("MEME_PROBABILITY", 0.40)

# ══════════════════ 群友关系档案（见 relations.py）══════════════════
AFFINITY_ENABLED = _env_bool("AFFINITY_ENABLED", True)
# 每轮是否让模型在回复末尾吐一行 <CMD>{"aff":n}</CMD>。
# 关掉（默认）后好感度改由每日压缩统一评估 —— 压缩看得到一整天完整对话，判得比每轮自评准，
# 且不再要求模型额外吐 JSON，注意力全留在角色上。想回退老行为设 CMD_PROTOCOL_ENABLED=true。
CMD_PROTOCOL_ENABLED = _env_bool("CMD_PROTOCOL_ENABLED", False)
# 压缩时一次性结算一整天的印象变化，幅度上限比单次互动（±3）放宽些
AFFINITY_DIGEST_SPAN = _env_int("AFFINITY_DIGEST_SPAN", 6)
AFFINITY_DECAY_GRACE_DAYS = _env_float("AFFINITY_DECAY_GRACE_DAYS", 3.0)  # 这几天不来往不扣
AFFINITY_DECAY_STEP_DAYS = _env_float("AFFINITY_DECAY_STEP_DAYS", 2.0)    # 之后每 N 天向 0 收敛一次
AFFINITY_DECAY_AMOUNT = _env_int("AFFINITY_DECAY_AMOUNT", 1)
AFFINITY_PRUNE_IDLE_DAYS = _env_float("AFFINITY_PRUNE_IDLE_DAYS", 90.0)   # 长期不互动就清掉档案
AFFINITY_BOARD_SIZE = _env_int("AFFINITY_BOARD_SIZE", 5)                  # 排行榜显示人数

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
# 摘要产出后顺手写一份 MD，人直接看这个；原始消息另存 JSONL 备查
DIGEST_WRITE_MD = _env_bool("DIGEST_WRITE_MD", True)

# ══════════════════ 原始消息归档（见 archive.py）═════════════════
# 摘要是有损压缩，出错时得有地方回查，所以原文单独按天留存。
ARCHIVE_DIR = _env_str("ARCHIVE_DIR", "archive")
MEMORY_DIR = _env_str("MEMORY_DIR", "memory")
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
PROMISE_QUIET_HOURS = {int(h) for h in re.findall(r"\d+", _env_str("PROMISE_QUIET_HOURS", "23,0,1,2,3,4,5,6,7,8"))}

# ══════════════════ 其它 ══════════════════
DEDUP_MAX = _env_int("DEDUP_MAX", 500)   # 消息去重 ID 队列长度
RECONNECT_MIN_DELAY = _env_float("RECONNECT_MIN_DELAY", 5.0)
RECONNECT_MAX_DELAY = _env_float("RECONNECT_MAX_DELAY", 300.0)
RESET_BACKOFF_AFTER = _env_float("RESET_BACKOFF_AFTER", 120.0)  # 稳定运行这么久就重置退避计数


def validate():
    """启动前校验，缺东西就 fail fast。"""
    errors = []
    if not QQ_APP_ID:
        errors.append("缺少 QQ_APP_ID（在 .env 里配置）")
    if not QQ_APP_SECRET:
        errors.append("缺少 QQ_APP_SECRET（在 .env 里配置）")
    if not AI_KEYS:
        errors.append(f"缺少 {PROVIDER['label']} 的 API Key（在 .env 或 api_keys_{AI_PROVIDER}.txt 里配置）")
    return errors
