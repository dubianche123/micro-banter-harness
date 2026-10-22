"""统一模型请求边界的离线回归：所有 HTTP 都由 MockTransport 处理。"""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

import providers
import config  # noqa: E402
from storage import DailyBudget


def response(content="答复", reasoning=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content, reasoning_content=reasoning))])


def fake_client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class BudgetTest(unittest.TestCase):
    def test_preflight_is_read_only_and_rollover_resets_usage(self):
        state = {"day": "2000-01-01", "used": 400}
        budget = DailyBudget(2, state)
        self.assertTrue(budget.can_consume())
        self.assertEqual(state, {"day": "2000-01-01", "used": 400})
        self.assertEqual(budget.remaining, 2)
        self.assertTrue(budget.try_consume())
        self.assertTrue(budget.try_consume())
        self.assertFalse(budget.can_consume())
        self.assertFalse(budget.try_consume())
        self.assertEqual(budget.remaining, 0)

    def test_negative_cost_cannot_refund_usage(self):
        budget = DailyBudget(2, {})
        with self.assertRaises(ValueError):
            budget.try_consume(-1)


class RequestsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = (list(providers.PROVIDER_CHAIN), dict(providers.AI_CLIENTS),
                      dict(providers._provider_state),
                      dict(providers._key_idx), providers._budget, providers._on_usage,
                      providers._initialized, providers._llm_http, dict(providers._TIER_CHAINS))
        providers.PROVIDER_CHAIN[:] = ["zhipu"]
        providers.AI_CLIENTS.clear()
        providers._provider_state.clear()
        providers._key_idx.clear()
        providers._initialized = True
        self.vision_options = patch.multiple(providers.config, VISION_PROVIDER="zhipu", VISION_MODEL="vision-test", VISION_INPUT_MODE="url")
        self.vision_options.start()
        self.state = {}
        self.budget = DailyBudget(10, self.state)
        self.dirty = []
        providers.initialize(budget=self.budget, on_usage=lambda: self.dirty.append(True))
        self.http_clients = []
        for tier in ("chat", "digest", "judge"):
            providers._TIER_CHAINS[tier] = {"zhipu": [f"{tier}-model"]}

    async def asyncTearDown(self):
        for client in self.http_clients:
            await client.aclose()
        self.vision_options.stop()
        chain, clients, state, indices, budget, on_usage, initialized, http, tiers = self.saved
        providers.PROVIDER_CHAIN[:] = chain
        for target, saved in ((providers.AI_CLIENTS, clients),
                              (providers._provider_state, state),
                              (providers._key_idx, indices)):
            target.clear(); target.update(saved)
        providers._budget, providers._on_usage = budget, on_usage
        providers._initialized, providers._llm_http = initialized, http
        providers._TIER_CHAINS.clear(); providers._TIER_CHAINS.update(tiers)

    def install_http(self, handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.http_clients.append(http)
        sdk = AsyncOpenAI(api_key="fake-local-test", base_url="https://model.invalid/v1",
                          http_client=http, max_retries=0)
        providers.AI_CLIENTS["zhipu"] = [sdk]
        return sdk

    async def test_failed_http_and_explicit_fallback_each_consume_once(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(500, json={"error": {"message": "unavailable"}})
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "备用模型答复"}}]})
        self.install_http(handler)
        providers._TIER_CHAINS["chat"] = {"zhipu": ["m1", "m2"]}
        got = await providers.call_model([{"role": "user", "content": "hello"}], 20)
        self.assertEqual(got, "备用模型答复")
        self.assertEqual(len(calls), 2, "SDK 不得私自重试，500 后只由显式链回退")
        self.assertEqual(self.budget.used, 2)
        self.assertEqual(len(self.dirty), 2)

    async def test_digest_judge_chat_share_one_budget(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs["model"])
            return response()
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        self.budget.limit = 2
        for tier in ("digest", "judge", "chat"):
            got = await providers.call_model([], 10, tier=tier)
            self.assertEqual(got, None if tier == "chat" else "答复")
        self.assertEqual(calls, ["digest-model", "judge-model"])
        self.assertEqual(self.budget.used, 2)
        self.assertEqual(providers._provider_state, {}, "预算耗尽不等于供应商故障")

    async def test_concurrent_requests_cannot_overspend(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            await asyncio.sleep(0)
            return response()
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        self.budget.limit = 2
        got = await asyncio.gather(*(providers.call_model([], 10) for _ in range(5)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(got.count("答复"), 2)
        self.assertEqual(self.budget.used, 2)

    async def test_reasoning_only_is_not_a_user_answer(self):
        async def create(**kwargs):
            return response(None, "private chain of thought")
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        self.assertIsNone(await providers.call_model([], 10))
        self.assertEqual(self.budget.used, 1)

    async def test_explicit_temperature_overrides_provider_preset(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return response()
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        with patch.multiple(providers.config, AI_TEMPERATURE=0.95,
                            AI_TEMPERATURE_EXPLICIT=True):
            self.assertEqual(await providers.call_model([], 10), "答复")
        self.assertEqual(calls[0]["temperature"], 0.95)

    async def test_explicit_download_input_sends_one_model_request(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return response("一只猫")
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"image", headers={"content-type": "image/png"})))
        self.http_clients.append(http)
        with patch.object(providers.httpx, "AsyncClient", return_value=http), \
             patch.object(providers.config, "VISION_INPUT_MODE", "download"):
            self.assertEqual(await providers.describe_image("https://image.invalid/a"), "一只猫")
        self.assertEqual(self.budget.used, 1)
        self.assertEqual(len(self.dirty), 1)
        self.assertEqual(calls[0]["max_tokens"], config.VISION_MAX_TOKENS)
        self.assertTrue(calls[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_vision_cannot_bypass_exhausted_daily_budget(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return response("图")
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        self.budget.limit = 0
        self.assertIsNone(await providers.describe_image("https://image.invalid/a"))
        self.assertEqual(calls, [])
        self.assertEqual(self.dirty, [])

    async def test_missing_keys_make_no_requests_and_keep_task_chains(self):
        providers.AI_CLIENTS.clear()
        providers._initialized = False
        with patch.object(providers.config, "load_api_keys", return_value=[]):
            self.assertIsNone(await providers.call_model([], 10))
            self.assertIsNone(await providers.describe_image("https://image.invalid/a"))
            cfg = providers.configuration()
            self.assertTrue(cfg["providers"]["zhipu"]["models"])
            self.assertTrue(cfg["providers"]["zhipu"]["digest_models"])
            self.assertFalse(providers.status()["providers"][0]["configured"])
            self.assertEqual(self.budget.used, 0)
            await providers.close()

    async def test_initialized_sdk_disables_hidden_retry(self):
        providers.AI_CLIENTS.clear()
        providers._initialized = False
        with patch.object(providers.config, "load_api_keys",
                          side_effect=lambda name: ["fake"] if name == "zhipu" else []):
            providers.initialize()
            self.assertEqual(providers.AI_CLIENTS["zhipu"][0].max_retries, 0)
            await providers.close()

    async def test_separate_vision_endpoint_initializes_alongside_chat(self):
        providers.AI_CLIENTS.clear()
        providers._initialized = False
        with patch.object(providers.config, "load_api_keys", return_value=["fake"]), \
             patch.multiple(providers.config, VISION_ENABLED=True, VISION_PROVIDER="gemini", VISION_MODEL="vision-only-model"):
            providers.initialize()
            self.assertEqual(set(providers.AI_CLIENTS), {"zhipu", "gemini"})
            # base_url 从 config preset 取预期（.env 可覆盖 ZHIPU_BASE_URL 指向本地网关）
            self.assertIn(str(config.PROVIDER_PRESETS["zhipu"]["base_url"]),
                          str(providers.AI_CLIENTS["zhipu"][0].base_url))
            self.assertIn("googleapis.com", str(providers.AI_CLIENTS["gemini"][0].base_url))
            await providers.close()

    async def test_vision_failure_is_not_replayed_as_an_implicit_download_retry(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("image URL rejected")
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        self.budget.limit = 1
        with patch.object(providers.httpx, "AsyncClient", side_effect=AssertionError("No image download expected")):
            self.assertIsNone(await providers.describe_image("https://image.invalid/a"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.budget.used, 1)

    async def test_slow_download_obeys_total_deadline_without_model_request(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return response("图")
        providers.AI_CLIENTS["zhipu"] = [fake_client(create)]
        async def slow_image(request):
            await asyncio.sleep(.3)
            return httpx.Response(200, content=b"image")
        http = httpx.AsyncClient(transport=httpx.MockTransport(slow_image))
        self.http_clients.append(http)
        with patch.object(providers.httpx, "AsyncClient", return_value=http), \
             patch.multiple(providers.config, VISION_INPUT_MODE="download", AI_TOTAL_TIMEOUT=.02):
            started = asyncio.get_running_loop().time()
            self.assertIsNone(await providers.describe_image("https://image.invalid/a"))
            self.assertLess(asyncio.get_running_loop().time()-started, .2)
        self.assertEqual(calls, [])
        self.assertEqual(self.budget.used, 0)

    async def test_inactive_malformed_endpoint_does_not_initialize_a_client(self):
        providers.AI_CLIENTS.clear()
        providers._initialized = False
        bad = dict(providers.config.PROVIDER_PRESETS["gemini"], base_url="http://[malformed")
        with patch.dict(providers.config.PROVIDER_PRESETS, {"gemini": bad}), \
             patch.object(providers.config, "load_api_keys", return_value=["fake"]), \
             patch.object(providers.config, "VISION_ENABLED", False):
            providers.initialize()
            self.assertEqual(set(providers.AI_CLIENTS), {"zhipu"})
            await providers.close()
