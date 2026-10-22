"""Bounded OpenAI-compatible calls: explicit candidates, typed errors, one budget.

Imports only describe configuration. The runtime owns persistence and explicitly
binds its DailyBudget; the SDK's hidden retries are disabled.
"""
import asyncio
import base64
from email.utils import parsedate_to_datetime
import logging
import time

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

import config

logger = logging.getLogger("qqbot")
_llm_http = None
_budget = None
_on_usage = None
_initialized = False
AI_CLIENTS = {}
PROVIDER_CHAIN = list(dict.fromkeys(filter(None, (config.AI_PROVIDER, config.FALLBACK_PROVIDER))))
MODEL_CHAINS = {name: list(preset["models"]) for name, preset in config.PROVIDER_PRESETS.items()}
MODEL_CHAINS_DIGEST = {name: list(preset.get("models_digest") or preset["models"])
                       for name, preset in config.PROVIDER_PRESETS.items()}
MODEL_CHAINS_JUDGE = {name: list(preset.get("models_judge") or preset["models"])
                      for name, preset in config.PROVIDER_PRESETS.items()}
_TIER_CHAINS = {"chat": MODEL_CHAINS, "digest": MODEL_CHAINS_DIGEST, "judge": MODEL_CHAINS_JUDGE}
_provider_state = {}  # only explicit Retry-After deadlines and credential failures
_key_idx = {}


def initialize(*, budget=None, on_usage=None):
    global _llm_http, _budget, _on_usage, _initialized
    if budget is not None:
        _budget = budget
    if on_usage is not None:
        _on_usage = on_usage
    if _initialized:
        return
    # 本地网关（127.0.0.1:17863，glm-5.3-flash）绝不能被环境代理劫持：
    # bot 进程带着 HTTP_PROXY 环境变量（launchd 铁律写死的），而 httpx 不像 curl
    # 有 localhost 内建绕过 —— 不豁免的话 loopback 目标会被塞给透明代理，
    # 代理不转发 loopback，表现为 ConnectError（2026-10-22 实测）。
    _llm_http = httpx.AsyncClient(verify=config.SSL_VERIFY,
                                  mounts={"all://127.0.0.1": None,
                                          "all://localhost": None})
    selected = list(PROVIDER_CHAIN)
    if config.VISION_ENABLED and config.VISION_PROVIDER not in selected:
        selected.append(config.VISION_PROVIDER)
    for name in selected:
        preset = config.PROVIDER_PRESETS[name]
        keys = config.load_api_keys(name)
        if keys and preset.get("base_url") and name not in AI_CLIENTS:
            AI_CLIENTS[name] = [AsyncOpenAI(api_key=key, base_url=preset["base_url"],
                                           http_client=_llm_http, max_retries=0)
                                for key in keys]
    _initialized = True


async def close():
    global _llm_http, _initialized
    if _llm_http is not None:
        await _llm_http.aclose()
    _llm_http = None
    AI_CLIENTS.clear()
    _provider_state.clear()
    _key_idx.clear()
    _initialized = False


def configuration():
    """Secret-free configuration shared by doctor and runtime status."""
    vision = config.PROVIDER_PRESETS[config.VISION_PROVIDER]
    return {
        "provider_chain": list(PROVIDER_CHAIN),
        "providers": {name: {
            "label": preset["label"], "models": list(MODEL_CHAINS[name]),
            "digest_models": list(MODEL_CHAINS_DIGEST[name]),
            "judge_models": list(MODEL_CHAINS_JUDGE[name]),
            "key_count": len(config.load_api_keys(name)),
        } for name, preset in config.PROVIDER_PRESETS.items()},
        "vision": {"provider": config.VISION_PROVIDER, "model": config.VISION_MODEL,
                   "input_mode": config.VISION_INPUT_MODE,
                   "configured": bool(vision.get("base_url") and config.VISION_MODEL
                                      and config.load_api_keys(config.VISION_PROVIDER))},
        "request_policy": {"max_attempts": config.AI_MAX_ATTEMPTS,
                           "timeout_seconds": config.AI_TIMEOUT_SECONDS,
                           "total_timeout_seconds": config.AI_TOTAL_TIMEOUT},

    }


def _available(name):
    state = _provider_state.get(name) or {}
    return not state.get("blocked") and time.time() >= state.get("cooldown_until", 0.0)


def status():
    cfg = configuration()
    rows = []
    for name in PROVIDER_CHAIN:
        row = dict(cfg["providers"][name], name=name)
        row["configured"] = bool(row["key_count"] and row["models"]
                                 and config.PROVIDER_PRESETS[name].get("base_url"))
        row["available"] = bool(AI_CLIENTS.get(name)) and _available(name)
        state = _provider_state.get(name) or {}
        row["blocked"] = state.get("blocked", "")
        row["cooldown_seconds"] = max(0.0, state.get("cooldown_until", 0.0) - time.time())
        rows.append(row)
    return {"initialized": _initialized, "providers": rows, "vision": cfg["vision"],
            "request_policy": cfg["request_policy"],
            "budget": ({"used": _budget.used, "limit": _budget.limit,
                        "remaining": _budget.remaining} if _budget is not None else None)}


class _BudgetExhausted(Exception):
    pass


def _bound_budget():
    global _budget, _on_usage
    if _budget is None:
        import runtime
        _budget, _on_usage = runtime.BUDGET, runtime.STATE.mark_dirty
    return _budget


async def _request_completion(client, **kwargs):
    """One SDK call = one reserved physical attempt, including failures."""
    if not _bound_budget().try_consume():
        raise _BudgetExhausted
    if _on_usage is not None:
        _on_usage()
    return await client.chat.completions.create(**kwargs)


def _retry_after(response):
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if delay < float("inf") else None


def _failure_action(name, exc):
    """Use protocol types/status only, never vendor prose or regex guesses."""
    if isinstance(exc, (APIConnectionError, asyncio.TimeoutError)):
        return "provider"  # a broken endpoint does not improve by changing model
    if not isinstance(exc, APIStatusError):
        return "stop"  # programming/unsupported request errors are not retry signals
    code = exc.status_code
    if code in (401, 403):
        _provider_state[name] = {"blocked": f"HTTP {code}"}
        return "provider"
    retry_after = _retry_after(exc.response)
    if code == 429 or (code == 503 and retry_after is not None):
        _provider_state[name] = {"cooldown_until": time.time() +
                                 (retry_after if retry_after is not None else 1.0)}
        if code == 429:
            _key_idx[name] = _key_idx.get(name, 0) + 1
        return "provider"
    if code in (404, 408, 409) or code >= 500:
        return "next"
    return "stop"


async def _complete(messages, max_tokens, tier, temperature, names, chains, *, total_timeout=None):
    initialize()
    deadline = time.monotonic() + (config.AI_TOTAL_TIMEOUT if total_timeout is None else total_timeout)
    attempts = 0
    for name in names:
        clients = AI_CLIENTS.get(name) or []
        if not clients or not _available(name):
            continue
        preset = config.PROVIDER_PRESETS[name]
        for model in chains.get(name) or []:
            left = deadline - time.monotonic()
            if attempts >= config.AI_MAX_ATTEMPTS or left <= 0:
                return None
            client = clients[_key_idx.get(name, 0) % len(clients)]
            attempts += 1
            try:
                result = await asyncio.wait_for(
                    _request_completion(
                        client, model=model, messages=messages, max_tokens=max_tokens,
                        temperature=(preset.get("temperature", config.AI_TEMPERATURE)
                                     if temperature is None else temperature),
                        extra_body=preset.get("extra_body") or None),
                    timeout=min(left, config.AI_TIMEOUT_SECONDS))
                if result and result.choices:
                    text = (getattr(result.choices[0].message, "content", None) or "").strip()
                    if text:  # reasoning_content is deliberately not a user answer
                        logger.info("✅ [%s] %s 应答 (%s, attempt %d)", name, model, tier, attempts)
                        return text
            except _BudgetExhausted:
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                action = _failure_action(name, exc)
                logger.warning("模型请求失败 provider=%s model=%s type=%s status=%s action=%s",
                               name, model, type(exc).__name__, getattr(exc, "status_code", None), action)
                if action == "stop":
                    return None
                if action == "provider":
                    break
    return None


async def call_model(messages, max_tokens, tier="chat", temperature=None):
    """Try only configured candidates, bounded by attempt/day/time budgets."""
    chains = _TIER_CHAINS.get(tier)
    if chains is None:
        raise ValueError(f"Unknown model task: {tier}")
    return await _complete(messages, max_tokens, tier, temperature, PROVIDER_CHAIN, chains)


async def describe_image(image_url, context_text=""):
    """Use one explicit vision model; image transport is configured, never guessed.

    context_text：图发出前的群聊记录（由 media.CaptionService 经 context_fn 注入）。
    多模态看图本来就该带着对话看——知道大家在聊什么，才知道这张图值得描述
    什么（2026-10-20 群主定调）。
    """
    initialize()
    name, model = config.VISION_PROVIDER, config.VISION_MODEL
    if not model or not AI_CLIENTS.get(name) or not _available(name) or not _bound_budget().can_consume():
        return None
    started = time.monotonic()
    if config.VISION_INPUT_MODE == "download":
        # 20s：QQ 图床大图经代理下载实测会超 12s（2026-10-22 08:42 女性图 TimeoutError，
        # 读图整轮失败）——下载是读图唯一的机会窗口，宁可多等。
        try:
            dl_timeout = min(20.0, config.AI_TOTAL_TIMEOUT)
            async with httpx.AsyncClient(verify=config.SSL_VERIFY,
                                          timeout=dl_timeout) as client:
                response = await asyncio.wait_for(
                    client.get(image_url), timeout=dl_timeout)
                response.raise_for_status()
            image_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            image_url = f"data:{image_type};base64," + base64.b64encode(response.content).decode()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("图片下载失败 type=%s", type(exc).__name__)
            return None
    remaining = config.AI_TOTAL_TIMEOUT - (time.monotonic() - started)
    if remaining <= 0:
        return None
    messages = [{"role": "user", "content": [
        *( [{"type": "text",
             "text": "这张图是发在下面这段群聊里的：\n" + context_text}]
           if context_text else [] ),
        {"type": "text", "text": "用不超过25字的中文客观描述图片内容或表情包情绪；"
                                 "图与聊天话题相关时，顺着话题描述大家关心的点。"
                                 "无法理解时只输出：无法描述"},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}]
    text = await _complete(messages, 200, "vision", 0.3, [name], {name: [model]},
                           total_timeout=remaining)
    return text[:80] if text and text != "无法描述" else None


async def probe_provider():
    """Optional metadata-only connection check; model-list presence does not prove usable capability."""
    initialize()
    name = config.AI_PROVIDER
    keys = config.load_api_keys(name)
    base_url = config.PROVIDER_PRESETS[name].get("base_url")
    if not keys or not base_url:
        return
    try:
        response = await _llm_http.get(base_url.rstrip("/") + "/models",
                                       headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15.0)
        logger.info("供应商元数据检查 provider=%s status=%s", name, response.status_code)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("供应商元数据检查失败 provider=%s type=%s", name, type(exc).__name__)
