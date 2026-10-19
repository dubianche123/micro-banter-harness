"""Protocol-based bounded fallback; every HTTP request stays in MockTransport."""
import asyncio
from email.utils import formatdate
import json
import time
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

import providers
from storage import DailyBudget


class GenericFailoverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = (list(providers.PROVIDER_CHAIN), dict(providers.AI_CLIENTS),
                      dict(providers._provider_state), dict(providers._key_idx),
                      dict(providers._TIER_CHAINS), providers._budget,
                      providers._on_usage, providers._initialized)
        providers.PROVIDER_CHAIN[:] = ["zhipu", "gemini"]
        providers.AI_CLIENTS.clear()
        providers._provider_state.clear()
        providers._key_idx.clear()
        providers._initialized = True
        providers._TIER_CHAINS["chat"] = {"zhipu": ["p1", "p2"], "gemini": ["f1"]}
        self.budget = DailyBudget(20, {})
        providers.initialize(budget=self.budget, on_usage=lambda: None)
        self.clients = []
        self.calls = []
        # Ordinary routing tests allow SDK lazy imports; deadline tests set their own short limits.
        self.settings = patch.multiple(providers.config, AI_MAX_ATTEMPTS=3,
                                        AI_TIMEOUT_SECONDS=5.0, AI_TOTAL_TIMEOUT=10.0)
        self.settings.start()

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.settings.stop()
        chain, clients, states, keys, tiers, budget, dirty, initialized = self.saved
        providers.PROVIDER_CHAIN[:] = chain
        for target, source in ((providers.AI_CLIENTS, clients), (providers._provider_state, states),
                               (providers._key_idx, keys), (providers._TIER_CHAINS, tiers)):
            target.clear(); target.update(source)
        providers._budget, providers._on_usage, providers._initialized = budget, dirty, initialized

    def install(self, name, handler):
        async def dispatch(request):
            self.calls.append((name, json.loads(request.content)["model"]))
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
            return result
        client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        self.clients.append(client)
        sdk = AsyncOpenAI(api_key="fake", base_url="https://test.invalid/v1",
                          http_client=client, max_retries=0)
        providers.AI_CLIENTS[name] = [sdk]
        return sdk

    @staticmethod
    def ok(text="ok"):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})

    @staticmethod
    def error(code, headers=None):
        return httpx.Response(code, headers=headers,
                              json={"error": {"message": "opaque message; no vendor phrase"}})

    async def run_call(self):
        return await providers.call_model([{"role": "user", "content": "hi"}], 20)

    async def test_primary_success_does_not_touch_fallback(self):
        self.install("zhipu", lambda request: self.ok())
        self.install("gemini", lambda request: self.ok("backup"))
        self.assertEqual(await self.run_call(), "ok")
        self.assertEqual(self.calls, [("zhipu", "p1")])

    async def test_500_tries_next_explicit_model_once(self):
        self.install("zhipu", lambda request: self.error(500) if len(self.calls) == 1 else self.ok())
        self.assertEqual(await self.run_call(), "ok")
        self.assertEqual(self.calls, [("zhipu", "p1"), ("zhipu", "p2")])
        self.assertEqual(self.budget.used, 2)

    async def test_connection_error_skips_endpoint_and_uses_explicit_fallback(self):
        def broken(request):
            raise httpx.ConnectError("opaque", request=request)
        self.install("zhipu", broken)
        self.install("gemini", lambda request: self.ok("backup"))
        self.assertEqual(await self.run_call(), "backup")
        self.assertEqual(self.calls, [("zhipu", "p1"), ("gemini", "f1")])

    async def test_authentication_error_does_not_rotate_keys_or_models(self):
        primary = self.install("zhipu", lambda request: self.error(401))
        providers.AI_CLIENTS["zhipu"].append(primary)
        self.install("gemini", lambda request: self.ok("backup"))
        self.assertEqual(await self.run_call(), "backup")
        self.assertEqual(await self.run_call(), "backup")
        self.assertEqual([name for name, model in self.calls], ["zhipu", "gemini", "gemini"])
        self.assertEqual(providers.status()["providers"][0]["blocked"], "HTTP 401")

    async def test_permission_error_also_blocks_only_that_endpoint(self):
        self.install("zhipu", lambda request: self.error(403))
        self.install("gemini", lambda request: self.ok())
        self.assertEqual(await self.run_call(), "ok")
        self.assertEqual(providers._provider_state["zhipu"]["blocked"], "HTTP 403")

    async def test_bad_request_is_not_blindly_replayed_to_other_models(self):
        self.install("zhipu", lambda request: self.error(400))
        self.install("gemini", lambda request: self.ok())
        self.assertIsNone(await self.run_call())
        self.assertEqual(self.calls, [("zhipu", "p1")])

    async def test_model_not_found_can_try_next_explicit_candidate(self):
        self.install("zhipu", lambda request: self.error(404) if len(self.calls) == 1 else self.ok())
        self.assertEqual(await self.run_call(), "ok")
        self.assertEqual(len(self.calls), 2)

    async def test_rate_limit_honors_retry_after_without_sleep_or_forced_probe(self):
        self.install("zhipu", lambda request: self.error(429, {"Retry-After": "60"}))
        self.install("gemini", lambda request: self.ok())
        self.assertEqual(await self.run_call(), "ok")
        self.assertEqual(await self.run_call(), "ok")
        self.assertEqual([name for name, model in self.calls].count("zhipu"), 1)
        self.assertGreater(providers.status()["providers"][0]["cooldown_seconds"], 58)

    async def test_retry_after_http_date_on_503_pauses_endpoint(self):
        self.install("zhipu", lambda request: self.error(503, {"Retry-After": formatdate(time.time()+60, usegmt=True)}))
        self.install("gemini", lambda request: self.ok())
        self.assertEqual(await self.run_call(), "ok")
        self.assertGreater(providers._provider_state["zhipu"]["cooldown_until"], time.time()+58)
        self.assertEqual(len(self.calls), 2)

    async def test_invalid_retry_after_uses_short_rate_limit_pause(self):
        self.install("zhipu", lambda request: self.error(429, {"Retry-After": "invalid"}))
        self.assertIsNone(await self.run_call())
        self.assertGreater(providers._provider_state["zhipu"]["cooldown_until"], time.time())

    async def test_cooling_endpoint_is_never_forced_as_last_resort(self):
        self.install("zhipu", lambda request: self.ok())
        providers._provider_state["zhipu"] = {"cooldown_until": time.time()+60}
        self.assertIsNone(await self.run_call())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.budget.used, 0)

    async def test_total_attempt_limit_applies_across_providers(self):
        self.install("zhipu", lambda request: self.error(500))
        self.install("gemini", lambda request: self.error(500))
        providers._TIER_CHAINS["chat"]["gemini"] = ["f1", "f2", "f3"]
        self.assertIsNone(await self.run_call())
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.budget.used, 3)

    async def test_total_timeout_bounds_even_a_stalled_transport(self):
        async def slow(request):
            await asyncio.sleep(1)
            return self.ok()
        self.install("zhipu", slow)
        self.install("gemini", slow)
        with patch.object(providers.config, "AI_TOTAL_TIMEOUT", .02):
            started = time.monotonic()
            self.assertIsNone(await self.run_call())
            self.assertLess(time.monotonic()-started, .2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.budget.used, 1)

    async def test_single_timeout_can_still_use_fallback_within_total_deadline(self):
        async def slow(request):
            await asyncio.sleep(.3)
            return self.ok()
        self.install("zhipu", slow)
        self.install("gemini", lambda request: self.ok("backup"))
        with patch.object(providers.config, "AI_TIMEOUT_SECONDS", .01):
            self.assertEqual(await self.run_call(), "backup")
        self.assertEqual(self.calls, [("zhipu", "p1"), ("gemini", "f1")])

    async def test_vision_uses_explicit_generic_provider_and_model(self):
        self.install("openai", lambda request: self.ok("cat"))
        with patch.multiple(providers.config, VISION_PROVIDER="openai", VISION_MODEL="vision-model", VISION_INPUT_MODE="url"):
            self.assertEqual(await providers.describe_image("https://image.invalid/cat"), "cat")
        self.assertEqual(self.calls, [("openai", "vision-model")])
        self.assertEqual(self.budget.used, 1)

    async def test_unknown_task_does_not_default_to_chat(self):
        self.install("zhipu", lambda request: self.ok())
        with self.assertRaises(ValueError):
            await providers.call_model([], 10, tier="typo")
        self.assertEqual(self.calls, [])
