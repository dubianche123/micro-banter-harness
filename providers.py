"""供应商容错：梯队链、熔断、慢模型处罚、双 Ended call_model（2026-10-06 从 bot.py 拆出）。

本模块自包含：只依赖 config 与自己的状态表，不引用任何运行时单例。
"""
import asyncio
import time

import httpx
import logging
logger = logging.getLogger("qqbot")   # 与 bot.py 同一个命名 logger，共享 handler
from openai import AsyncOpenAI

import config

# ══════════════════════ 2. AI 客户端 ══════════════════════

_llm_http = httpx.AsyncClient(verify=config.SSL_VERIFY)
ai_clients = [
    AsyncOpenAI(api_key=k, base_url=config.PROVIDER["base_url"], http_client=_llm_http)
    for k in config.AI_KEYS
]
client_idx = 0
CANDIDATE_MODELS = list(config.CANDIDATE_MODELS)

# 供应商梯队：主供应商打不通时依次回落。每家各自持有一组 client（对应各自的 Key 池）。
PROVIDER_CHAIN = [config.AI_PROVIDER]
if config.FALLBACK_PROVIDER and config.FALLBACK_PROVIDER not in PROVIDER_CHAIN:
    PROVIDER_CHAIN.append(config.FALLBACK_PROVIDER)

AI_CLIENTS = {}
MODEL_CHAINS = {}         # 供应商 -> 对话梯队（要好）
MODEL_CHAINS_DIGEST = {}  # 供应商 -> 压缩/批处理梯队（要便宜、要能吞原始聊天）
MODEL_CHAINS_JUDGE = {}   # 供应商 -> 称呼审核梯队（要认得出谐音、拆字、暗指）
for _name in PROVIDER_CHAIN:
    _keys = config.load_api_keys(_name)
    if not _keys:
        continue
    AI_CLIENTS[_name] = [
        AsyncOpenAI(api_key=k, base_url=config.PROVIDER_PRESETS[_name]["base_url"],
                    http_client=_llm_http)
        for k in _keys
    ]
    _preset = config.PROVIDER_PRESETS[_name]
    MODEL_CHAINS[_name] = list(_preset["models"])
    # 压缩梯队刻意**不追加**对话梯队：两个都挂了就等下一轮，消息留在归档里不会丢
    # （run_digest 失败时不会推进 last_run）。宁可晚点压，也不要拿敏感 transcript 去撞强模型的过滤器。
    MODEL_CHAINS_DIGEST[_name] = (list(_preset.get("models_digest") or [])
                                  or list(_preset["models"]))
    # 审核梯队同理独立：这活只有强档干得了，用弱档等于没开（实测 4.5-air 会漏谐音、还误杀正常昵称）
    MODEL_CHAINS_JUDGE[_name] = (list(_preset.get("models_judge") or [])
                                 or list(_preset["models"]))

# tier 名 -> 梯队。加新梯队只要在这里登记一行，call_model 不用动。
_TIER_CHAINS = {
    "chat": MODEL_CHAINS,
    "digest": MODEL_CHAINS_DIGEST,
    "judge": MODEL_CHAINS_JUDGE,
}

if len(PROVIDER_CHAIN) > 1 and len(AI_CLIENTS) > 1:
    print(f"🔀 供应商梯队：{' → '.join(config.PROVIDER_PRESETS[n]['label'] for n in PROVIDER_CHAIN)}")

# 供应商熔断：连续失败就暂时拉黑，免得代理断了之后每条消息都干等一个超时周期
_provider_state = {}   # name -> {"fails": int, "cooldown_until": float}
_key_idx = {}          # name -> 轮到第几个 Key
# 单模型「慢」处罚：(供应商, 模型名) -> 到期时间戳。超时过的一档在这个时间前会被跳过，
# 免得它占着队首让每一轮都先白等一个满超时 —— 见 _penalize_slow_model。
_slow_until = {}
# 最近一条群消息的时刻。它决定「被让位的供应商什么时候能回来」—— 见 _provider_available。
# 内存态即可：重启后当作「群刚静下来」，立刻重试一次主供应商，代价只有一次 prefill。
_activity = {"ts": 0.0}
# 不可恢复错误：连接层（代理断、DNS 挂）+ 地区/权限类。它们跟配额错误不同 ——
# 换模型、换 Key 都救不回来，必须立刻熔断换供应商，
# 否则几个模型逐个试一遍，用户要白等十几秒。
#
# ⚠️ 判定时用的是 f"{异常类名}: {消息}" 并统一转小写，不是光看 str(e)。因为连接层错误的
#    字符串往往只有一句 "Connection error."，类名反而只存在于 type(e).__name__ 里 ——
#    而「代理挂了」正是靠 APIConnectionError 才认得出来（实测 str(e) 里不含任何标记串）。
#    各家 SDK 的大小写不一致（API key not valid / api_key_invalid），所以标记一律小写。
_HARD_FAIL_MARKS = (
    "connecterror", "proxyerror", "remoteprotocolerror", "apiconnectionerror",
    # 超时：httpx 会抛 ConnectTimeout/ReadTimeout/PoolTimeout，但 SDK 通常把它们
    # 统一包成 APITimeoutError 再抛出，所以这个类名才是实际会撞上的那个。
    "apitimeouterror", "connecttimeout", "readtimeout", "pooltimeout",
    "connection error",
    "connection refused", "upstream connect failed", "connect call failed",
    "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "connection reset", "network is unreachable",
    # 地区/凭证类：Gemini 对某些出口 IP 直接返回 400/403，且短时间内不会变
    "location is not supported", "not supported for the api use",
    "api key not valid", "api_key_invalid", "invalid api key", "permission denied",
)

# 「结构性故障」＝上面的硬失败里，那种**等一分钟也不会好**的子集：地区封禁、Key 失效。
# 它们和抖动的区别不是严重等级，而是**会不会自己恢复** —— 代理抖一下几十秒就回来，
# 而 Gemini 的 400 "User location is not supported" 是出口 IP 决定的，两分钟后再试还是同一个错。
#
# 实测（2026-09-17 02:15 → 09-18 15:45，见日志复盘）：这一类 400 在一天半里撞了 **39 次**，
# 每两分钟冷却一到期就被放回来重撞一轮（每次还会顺着模型梯队连试 3 档），
# 全员熔断时甚至被 `_pick_last_resort` 当成「冷却剩余最短」的那家挑去带伤上阵 ——
# 明知道它结构性不可用还让它出工。所以这类错要单独记一笔长的隔离窗，并且在里面
# 不许它在「带伤上阵」里跟别家抢。
_FATAL_FAIL_MARKS = (
    "location is not supported", "not supported for the api use",
    "api key not valid", "api_key_invalid", "invalid api key", "permission denied",
)

async def probe_provider():
    """启动时单次探活：只做 TLS 握手 + 校验 Key/模型可用性，全程 0 token 消耗。

    刻意【不调 chat 接口】：/chat/completions 哪怕 max_tokens=5 也是真实计费并占 RPM；
    而 GET /models 免费，同样能达到「建连接池 + 验凭证 + 验模型名」三个目的。
    """
    url = config.PROVIDER["base_url"].rstrip("/") + "/models"
    logger.info("🔌 正在探活 [%s] 连接（0 token 消耗）...", config.PROVIDER["label"])
    try:
        t0 = time.time()
        resp = await _llm_http.get(
            url,
            headers={"Authorization": f"Bearer {config.AI_KEYS[client_idx % len(config.AI_KEYS)]}"},
            timeout=15.0,
        )
        elapsed = time.time() - t0
        if resp.status_code != 200:
            logger.warning("⚠️ 探活返回异常状态码 %s: %s", resp.status_code, resp.text[:120])
            return
        available = {m.get("id") for m in (resp.json().get("data") or []) if isinstance(m, dict)}
        # Gemini 的 id 带 "models/" 前缀，智谱不带，剥掉再比，否则会误报模型不存在
        available |= {i.rsplit("/", 1)[-1] for i in available}
        logger.info(
            "✅ 连接预热完成（耗时 %.2fs，含 TLS 握手）| 平台可用模型 %d 个", elapsed, len(available)
        )
        # /models 只收录付费档（此刻 10 个），flash 档常年不在其中却照样能调 ——
        # 「不在列表」不等于「不可用」，所以这里只能提示、不能断言失败。
        # 真正的可用性判据是调用时的 429/1113，交给 failover 梯队接管。
        missing = [m for m in CANDIDATE_MODELS if available and m not in available]
        if missing:
            logger.info(
                "ℹ️ 配置的模型 %s 未出现在 /models 列表 —— 该列表只收录付费档，flash 档常年不在其中，"
                "不代表不可调用；若确已下线，调用会自动回落到梯队下一档", missing
            )
    except Exception as e:
        logger.warning("⚠️ 探活跳过（不影响运行）: %s", str(e)[:120])


def _provider_available(name, allow_yield=True):
    """这家现在能不能用。两条都满足才行：

    1) 熔断冷却过了 —— 失败之后的最短让位时间；
    2) **群已经静下来够久，缓存大概率凉了**（`allow_yield=False` 时跳过这条，
       只看熔断 —— 全员让位时用它破死锁，见 call_model）。

    第 2 条是「缓存优先」的核心。被让位的那家前缀缓存还热着的时候切回去，等于把
    已经付过钱的 prefill 白扔：换一家就要把整段稳定头 + 历史重新算一遍，而群聊的
    每一条消息都紧挨着上一条，命中一次就够本。所以群里还在聊就一直留在正在服务
    的那家；等群静到超过缓存时效，切换才是免费的，这时候才回去重试。

    例外（很重要）：只有「别家顶得上」时才让位。备用也挂了就必须让主供应商自己上，
    否则一级熔断 + 一级失效 = 整条链全哑，宁可多花点 prefill 也不能不说话。
    """
    st = _provider_state.get(name)
    if not st:
        return True
    now = time.time()
    if now < st.get("cooldown_until", 0.0):
        return False
    if allow_yield and now - _last_msg_ts() < config.PROVIDER_CACHE_WARM_SECONDS:
        if any(p != name and _provider_serving(p) for p in PROVIDER_CHAIN):
            return False
    return True


def _penalize_slow_model(pname, model_name):
    """给「刚超时过」的这一档记一笔，让它在 MODEL_SLOW_PENALTY_SECONDS 内先靠边站。

    不是永久降级：记的是**到期时间戳**，凉够了自己回到原位试运行 —— 答上来就留用，
    再超时就再罚一轮。写的是时间而不是改梯队顺序，是因为顺序变了就回不去了：
    晚高峰变慢的模型，过一小时可能又快又好用，把它永久沉到队尾等于白丢一档质量。
    """
    _slow_until[(pname, model_name)] = time.time() + config.MODEL_SLOW_PENALTY_SECONDS
    logger.warning("🐢 [%s] %s 这一档暂时挂免战牌（%.0f 分钟内先跳过去）",
                   config.PROVIDER_PRESETS[pname]["label"], model_name,
                   config.MODEL_SLOW_PENALTY_SECONDS / 60.0)


def _model_slow(pname, model_name):
    """这一档现在是不是还在「慢」的处罚期里。"""
    return time.time() < _slow_until.get((pname, model_name), 0.0)


def _someone_else_can_serve(name, chains):
    """除这家以外，还有别人能出工吗（不看缓存、只看熔断）。

    用来回答「这一脚我可以踹多重」：有别家兜着时，单个模型超时就该立刻把整家摁下去
    （它后面的模型大概率也是一样的慢，别让用户白等）；**没人兜着时**就得手下留情 ——
    整家拉黑等于这一次彻底没话说，而剩下的便宜模型很可能立刻就答上来了。
    """
    return any(
        p != name and AI_CLIENTS.get(p) and chains.get(p)
        and _provider_available(p, allow_yield=False)
        for p in PROVIDER_CHAIN)


def _pick_last_resort(chains):
    """全员真熔断时挑一家「带伤上阵」—— 冷却剩余最短的那家。

    破锁只解决了「互让」，解决不了「两家都在冷却」。实测事故（2026-09-18 14:18）：
    智谱偶发一次 20s 超时被关 120s，而 Gemini 正卡在地区不可用上，于是整整两分钟
    机器人只会回「刚才走神了」。**这时候沉默比多等一次更糟** —— 让最可能已经恢复
    的那家再试一次，成了就成，不成也只是多等一轮。
    """
    def _still_fatal(p):
        """这家是不是还戴着「结构性出局」的帽子（地区封禁 / Key 失效）。"""
        return time.time() < (_provider_state.get(p) or {}).get("fatal_until", 0.0)

    cands = [p for p in PROVIDER_CHAIN if AI_CLIENTS.get(p) and (chains.get(p))]
    if not cands:
        return None

    # 结构性出局的那家不许跟别家抢「带伤上阵」的机会：它只是恰好冷却剩得短，
    # 而它这轮必挂（实测 14:53：让 [Google Gemini] 带伤上阵 → 立刻又一个
    # "User location is not supported"，白等一轮）。只有别无选择时才轮到它。
    healthy = [p for p in cands if not _still_fatal(p)]
    pool = healthy or cands
    return min(pool,
               key=lambda p: (_provider_state.get(p) or {}).get("cooldown_until", 0.0))


def _provider_serving(name):
    """这家现在顶得上吗：有 client、且不在熔断冷却里。"""
    if not AI_CLIENTS.get(name):
        return False
    st = _provider_state.get(name) or {}
    return time.time() >= st.get("cooldown_until", 0.0)


def _provider_block_reason(name):
    """让位的原因，只用于日志 —— 这两种「不可用」的含义完全不同，别混着报。"""
    st = _provider_state.get(name) or {}
    if time.time() < st.get("cooldown_until", 0.0):
        left = st["cooldown_until"] - time.time()
        # 冷却原因要分开说：这两种不可用**能不能靠等**完全不同 —— 前者等一分钟可能就回来了，
        # 后者（地区封禁/凭证失效）等多久都一样，看到它就别再盼它恢复了。
        if time.time() < st.get("fatal_until", 0.0):
            return f"结构性故障（地区/凭证），隔离中，还剩 {left:.0f}s"
        return f"熔断中，还剩 {left:.0f}s"
    idle = time.time() - _last_msg_ts()
    return f"缓存还热（群 {idle:.0f}s 前还在聊 < {config.PROVIDER_CACHE_WARM_SECONDS:.0f}s），先不切回去"


def _last_msg_ts():
    """最近一条群消息的时刻。缓存还热不热，看的就是它。"""
    return _activity["ts"]


def _note_activity():
    """收到群消息就记一笔。让被让位的供应商一直等到群静下来才回来。"""
    _activity["ts"] = time.time()


def _is_hard_fail(exc):
    """这个异常是不是「换模型、换 Key 都救不回来」的那种。

    必须连异常类名一起看：连接层错误的 str(e) 往往只有一句 "Connection error."，
    关键词一个都不出现，类名只存在于 type(e).__name__ 里 —— 而「代理断了」正是
    靠 APIConnectionError 才认得出来。只看 str(e) 的话，代理一断还要连撞满阈值
    才肯换供应商，用户得白等好几轮。
    """
    return any(k in f"{type(exc).__name__}: {exc}".lower() for k in _HARD_FAIL_MARKS)


def _is_fatal_fail(exc):
    """这类错是不是「等冷却过了也不会好」的那种（地区封禁 / Key 失效）。

    是 `_is_hard_fail` 的子集：hard 说的是「这家整条链路现在不通，直接拉黑」，
    fatal 说的是「它不是在抖，它是出局了」——所以隔离窗要长得多，见 `config
    .PROVIDER_FATAL_COOLDOWN_SECONDS`。判定串同样走「类名 + 消息」并小写，
    跟 `_is_hard_fail` 保持一致。
    """
    return any(k in f"{type(exc).__name__}: {exc}".lower() for k in _FATAL_FAIL_MARKS)


def _note_provider_fail(name, hard=False, fatal=False):
    """记一次失败。hard=连接层错误，直接拉黑，不等够阈值。

    fatal=地区封禁/凭证失效这类「等也不会好」的错：不看失败次数，直接关进长隔离窗，
    并记下 `fatal_until` —— 「冷却剩多久」和「是不是结构性出局」是两件事，
    `_pick_last_resort` 要靠后者判断该不该让它带伤上阵（见上面 14:53 那次教训）。
    """
    st = _provider_state.setdefault(name, {"fails": 0, "cooldown_until": 0.0})
    now = time.time()
    label = config.PROVIDER_PRESETS[name]["label"]
    if fatal:
        st["fails"] = 0
        st["cooldown_until"] = now + config.PROVIDER_FATAL_COOLDOWN_SECONDS
        st["fatal_until"] = st["cooldown_until"]
        logger.warning("🚫 供应商 [%s] 结构性故障（地区封禁或凭证失效），隔离 %.0f 分钟 "
                       "—— 这类错不会因为等多久而好转，别每两分钟回来重撞一次",
                       label, config.PROVIDER_FATAL_COOLDOWN_SECONDS / 60.0)
        return
    st["fails"] += config.PROVIDER_FAIL_THRESHOLD if hard else 1
    if st["fails"] >= config.PROVIDER_FAIL_THRESHOLD:
        st["fails"] = 0
        st["cooldown_until"] = time.time() + config.PROVIDER_COOLDOWN_SECONDS
        logger.warning("🚧 供应商 [%s] 暂时让位（至少 %.0f 秒；群里一直在聊就先留在别家吃缓存，"
                       "等群静下来再回来重试）",
                       config.PROVIDER_PRESETS[name]["label"], config.PROVIDER_COOLDOWN_SECONDS)


def _note_provider_ok(name):
    """应答成功 = 这家已经回来，连带把结构性出局的标记一起清掉。

    ⚠️ 不能只清 `cooldown_until`：`fatal_until` 是另一件事留的（「别让它带伤上阵」），
    漏清会在它明明已经恢复正常后仍然被排除在「最后人选」之外。
    """
    st = _provider_state.get(name)
    if st:
        st["fails"] = 0
        st["cooldown_until"] = 0.0
        st["fatal_until"] = 0.0


async def call_model(messages, max_tokens, tier="chat", temperature=None):
    """供应商 → 模型梯队双层尝试：先在主供应商内换模型/换 Key，全挂了再回落备用供应商。

    tier="chat"   走对话梯队（4.7 优先，要好）；
    tier="digest" 走压缩梯队（4.5-air 优先，要便宜、要能吞原始聊天）；
    tier="judge"  走审核梯队（只放最强的，要认得出谐音/拆字/暗指，弱档在这里等于没开）。

    temperature 不给就用 config.AI_TEMPERATURE（对话要的就是那点随机性，0.9 是角色需要）。
    **判断类任务必须显式传 0** —— 实测同一个词、同一个模型，0.9 下判 OK、0.0 下判 NG，
    审核这种要的是稳定复现，不是灵气。

    分工切在「任务」而不是「快慢」上，是因为：压缩的 prompt 与聊天不共享任何前缀，
    换模型零缓存损失；压缩在后台跑不怕慢；压缩不参与人格。
    而按「插嘴/正经」切会让同一个角色在不同路径上表现不一致，且实测插嘴一天 0 次，不值。

    注意：**缓存是按模型隔离的**，同一个模型反复用才吃得到缓存，换模型要重新 prefill。

    跨供应商降级是给「代理断了」这类整条链路不通的场景兜底的 —— 这时光换模型没用，
    必须换一家。熔断是为了避免主供应商挂掉后每条消息都白等一个超时周期。

    但「什么时候切回来」不是冷却是多久说了算，而是**缓存**说了算：被让位那家的前缀
    还热着就先别回去（回去要重新 prefill 一整段），等群静到超过缓存时效再重试。
    见 _provider_available —— 那条规则同时保证「别家也挂了时自己必须顶上」。
    """
    start_time = time.time()
    chains = _TIER_CHAINS.get(tier) or MODEL_CHAINS

    # ⚠️ 「缓存优先」会自锁：A 看到 B 顶得上就让位，B 看到 A 顶得上也让位 ——
    # 两家都不出工，整条链全哑。症状极具迷惑性：进程活着、消息收得到、也确实回了，
    # 只是回的全是「刚才走神了」这类兜底话术，**看起来就像宕机**。
    # 所以开跑前先问一句「真有人能顶上吗」，没有就关掉让位（只按熔断挑），
    # 宁可多花一次 prefill 也不能不说话 —— 这正是上面那条例外的本意。
    allow_yield = any(
        AI_CLIENTS.get(p) and (chains.get(p)) and _provider_available(p)
        for p in PROVIDER_CHAIN)
    if not allow_yield:
        logger.info("🚨 全员让位（互让死锁），本轮关闭缓存让位，只按熔断挑供应商")

    # 连「只按熔断挑」都没人上 = 全员真熔断。这时谁都不出工，整条链等于哑了，
    # 让冷却剩余最短的那家带伤上阵（见 _pick_last_resort）。
    last_resort = None
    if not any(AI_CLIENTS.get(p) and (chains.get(p))
               and _provider_available(p, allow_yield=False) for p in PROVIDER_CHAIN):
        last_resort = _pick_last_resort(chains)
        if last_resort:
            logger.warning(
                "🩹 全员熔断，让 [%s] 带伤上阵 —— 沉默比多等一次更糟",
                config.PROVIDER_PRESETS[last_resort]["label"])

    for pname in PROVIDER_CHAIN:
        elapsed = time.time() - start_time
        if elapsed >= config.AI_TOTAL_TIMEOUT:
            break
        clients = AI_CLIENTS.get(pname) or []
        # 刻意用共享引用而不是拷贝：下面把配额耗尽的模型沉到队尾，这个顺序要跨调用保留，
        # 否则每条消息都会先去撞一次已知 429 的模型，白等一轮。
        models = chains.get(pname) or []
        if not clients or not models:
            continue
        if pname != last_resort and not _provider_available(pname, allow_yield=allow_yield):
            logger.info("⏭️ 供应商 [%s] 让位中（%s），先用别家",
                        config.PROVIDER_PRESETS[pname]["label"], _provider_block_reason(pname))
            continue

        preset = config.PROVIDER_PRESETS[pname]
        extra_body = preset.get("extra_body") or None
        for model_name in list(models):
            elapsed = time.time() - start_time
            if elapsed >= config.AI_TOTAL_TIMEOUT:
                break
            # 熔断可能在上一个模型失败时刚触发，这时没必要再试这家剩下的模型。
            # 这里只看熔断不看缓存：同一家内部换模型不涉及「切回去要重新 prefill」。
            # 带伤上阵的那家例外 —— 它本来就是越过熔断挑的。
            if pname != last_resort and not _provider_available(pname, allow_yield=False):
                break
            # 刚超时过的那一档先靠边站（见 _penalize_slow_model）。⚠️ 前提是这家至少还有一档
            # 没被罚 —— 全罚了还硬躲就是自己把自己饿死，那时候只能照常试。
            if _model_slow(pname, model_name) and not all(
                    _model_slow(pname, m) for m in models):
                logger.info("⏭️ 跳过 [%s] %s（%.0fs 内它刚超时过，先用快的那档）",
                            preset["label"], model_name,
                            (_slow_until[(pname, model_name)] - time.time()))
                continue
            timeout_for_this = min(config.AI_TOTAL_TIMEOUT - elapsed,
                                   preset.get("timeout") or config.AI_TIMEOUT_SECONDS)
            idx = _key_idx.get(pname, 0)
            current_client = clients[idx % len(clients)]
            try:
                response = await asyncio.wait_for(
                    current_client.chat.completions.create(
                        model=model_name,
                        messages=messages,
                        max_tokens=max_tokens,
                        # 显式用 is None 判断，不能写 `temperature or ...` —— 0.0 是合法取值却被当成缺省
                        temperature=config.AI_TEMPERATURE if temperature is None else temperature,
                        extra_body=extra_body,
                    ),
                    timeout=timeout_for_this,
                )
                if response and response.choices:
                    msg = response.choices[0].message
                    # 推理模型偶尔把内容全塞进 reasoning_content 而 content 为空，这里兜一层
                    candidate = (getattr(msg, "content", None) or getattr(msg, "reasoning_content", None) or "").strip()
                    if candidate:
                        _note_provider_ok(pname)
                        if pname != config.AI_PROVIDER:
                            logger.info("↩️ 本次由备用供应商 [%s] 应答", preset["label"])
                        else:
                            # 主供应商应答也要留痕：2026-09-17 统计「今天谁在回话」时
                            # 发现只能靠「总数 - 备用数 - 兜底数」倒推，非常别扭。
                            logger.info("✅ [%s] %s 应答（%s）",
                                        preset["label"], model_name, tier)
                        return candidate
            except asyncio.TimeoutError:
                logger.warning("⏰ [%s] %s 超过 %.1fs，尝试下一个...",
                               preset["label"], model_name, timeout_for_this)
                # ⚠️ 超时是**这一档自己的事**：4.7 卡住不代表 4.5-air 也答不上来。
                # 但光「去找下一档」是不够的 —— 下一轮它又排在队首，于是每一轮都先白等 20s，
                # 而整轮预算只有 AI_TOTAL_TIMEOUT（30s），剩下的时间常常连第二档都跑不完
                # ⇒ 用户看到的就是连续几句「刚才走神了」。
                # 实测（2026-09-18 21:38 / 22:27 / 22:28 / 22:29）：glm-4.7 一小时里超时 4 次，
                # 每次拖垮整轮，还顺手把整家供应商送进 120s 冷却，把后面几句一起赔进去。
                # 所以给这一档记个「慢」的处罚：这段时间里梯队先跳过它，等它凉够了自己回来。
                _penalize_slow_model(pname, model_name)
                # 至于是不是要连坐整家，跟别的失败同一个规矩：有人兜底才狠，没人兜底就轻手。
                _note_provider_fail(pname, hard=_someone_else_can_serve(pname, chains))
            except Exception as e:
                err_msg = str(e)
                logger.warning("⚠️ [%s] %s 异常: %s，尝试下一个...",
                               preset["label"], model_name, err_msg[:100])
                if any(k in err_msg for k in ("429", "RESOURCE_EXHAUSTED")):
                    # 配额/限流：换 Key 继续，并把耗尽的模型沉到队尾
                    _key_idx[pname] = idx + 1
                    if model_name in models:
                        models.remove(model_name)
                        models.append(model_name)
                else:
                    # fatal 是 hard 的子集：先用宽松的 hard 判「要不要立刻拉黑」，
                    # 再用严格的 fatal 判「这一觉要多长」。
                    fatal = _is_fatal_fail(e)
                    hard = _is_hard_fail(e)
                    # ⚠️ 单模型超时 ≠ 整家断线：4.7 慢起来不代表 4.5-air 也答不上来。
                    # 但**有别家兜着**时可以狠一点（一次就把这家摁下去，免得每档都白等一轮）；
                    # **没人兜着**时必须换轻手 —— 此时把它整家拉黑 = 这一次彻底没话说，
                    # 而它梯队里剩下的便宜模型很可能一两句就答上来了。
                    # 实测场景（2026-09-18 16:11）：Gemini 正处结构性隔离，智谱 4.7 一次 20s
                    # 超时 → 整家立刻降温 120s，4.5-air / 4-flash 连试都没试，用户直接吃掉
                    # 一句兜底话术。总超时 AI_TOTAL_TIMEOUT 仍在兜着最坏情况。
                    if hard and not fatal and not _someone_else_can_serve(pname, chains):
                        _note_provider_fail(pname)  # 降级为普通失败：记一笔，凑够阈值才禁
                    else:
                        _note_provider_fail(pname, hard=hard, fatal=fatal)
        logger.warning("⤵️ 供应商 [%s] 全部模型不可用，回落到下一家", preset["label"])
    return None
