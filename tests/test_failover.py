"""跨供应商降级回归测试。

验证两件事：
1. 主供应商正常时，走主供应商应答。
2. 主供应商整条链路不通（模拟代理断了）时，自动回落备用供应商，并且熔断生效
   —— 熔断是关键，否则主供应商挂掉后每条消息都要白等一个超时周期。
"""
import asyncio
import sys
import time
import unittest

sys.path.insert(0, "..")

import config


class FailoverTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        global bot
        import bot  # noqa: E402
        cls.bot = bot

    def setUp(self):
        self.bot._provider_state.clear()
        self.bot._key_idx.clear()

    async def test_primary_answers(self):
        """主供应商可用时，应答来自主供应商（熔断记录为空）。"""
        if not self.bot.AI_CLIENTS.get(config.AI_PROVIDER):
            self.skipTest("主供应商没有可用 Key")
        t0 = time.time()
        reply = await self.bot.call_model(
            [{"role": "user", "content": "用一句话打招呼"}], 200)
        self.assertTrue(reply and reply.strip(), "主供应商应当能正常应答")
        self.assertEqual(self.bot._provider_state.get(config.AI_PROVIDER, {}).get("fails", 0), 0)
        print(f"\n    主供应商 {time.time() - t0:.1f}s → {reply[:60]!r}")

    async def test_falls_back_when_primary_dead(self):
        """主供应商连不上时，回落到备用供应商，且主供应商被熔断。"""
        if len(self.bot.PROVIDER_CHAIN) < 2:
            self.skipTest("未配置备用供应商")
        primary = config.AI_PROVIDER
        fallback = config.FALLBACK_PROVIDER
        if not self.bot.AI_CLIENTS.get(fallback):
            self.skipTest("备用供应商没有可用 Key")

        # 把主供应商的 base_url 指到一个必然连不上的端口，模拟代理中断
        saved = []
        for c in self.bot.AI_CLIENTS[primary]:
            saved.append(c.base_url)
            c.base_url = "http://127.0.0.1:59999/v1"
        try:
            t0 = time.time()
            reply = await self.bot.call_model(
                [{"role": "user", "content": "用一句话打招呼"}], 200)
            dt = time.time() - t0
            self.assertTrue(reply and reply.strip(), "主供应商挂了应当自动回落备用")
            st = self.bot._provider_state.get(primary, {})
            self.assertGreater(st.get("cooldown_until", 0), time.time(),
                               "主供应商连接层失败应当被立即熔断")
            # 熔断的意义就是别让用户干等，所以整轮不能拖到超时上限
            self.assertLess(dt, config.AI_TOTAL_TIMEOUT,
                            f"降级耗时 {dt:.1f}s 不应接近总超时 {config.AI_TOTAL_TIMEOUT}s")
            print(f"\n    降级 {dt:.1f}s → {reply[:60]!r}（主供应商已熔断）")
        finally:
            for c, url in zip(self.bot.AI_CLIENTS[primary], saved):
                c.base_url = url

    async def test_cooldown_skips_primary(self):
        """冷却期内的主供应商会被直接跳过，不再逐个模型重试。"""
        if len(self.bot.PROVIDER_CHAIN) < 2:
            self.skipTest("未配置备用供应商")
        primary = config.AI_PROVIDER
        self.bot._provider_state[primary] = {
            "fails": 0, "cooldown_until": time.time() + 600}
        self.assertFalse(self.bot._provider_available(primary))
        self.assertTrue(self.bot._provider_available(config.FALLBACK_PROVIDER))

    async def test_success_clears_cooldown(self):
        """主供应商成功一次就要解除熔断，不然会一直被误判为不可用。"""
        st = {"fails": 2, "cooldown_until": time.time() + 600}
        self.bot._provider_state["zhipu"] = st
        self.bot._note_provider_ok("zhipu")
        self.assertEqual(st["fails"], 0)
        self.assertEqual(st["cooldown_until"], 0.0)

    # ── 让位多久：缓存说了算，不是冷却说了算 ──

    def test_warm_cache_keeps_primary_benched(self):
        """群还在热聊时，冷却过了也不回主供应商 —— 它那边的缓存还热着，切回去要重新 prefill。

        这正是「缓存优先」的落地：一次抖动之后不是 120 秒就急着切回来，而是等到群静下来
        （超过 PROVIDER_CACHE_WARM_SECONDS）才回去重试，那时切换才是免费的。
        """
        if len(self.bot.PROVIDER_CHAIN) < 2:
            self.skipTest("未配置备用供应商")
        primary = config.AI_PROVIDER
        # 冷却已经走完（不然测的还是老规则）
        self.bot._provider_state[primary] = {"fails": 0, "cooldown_until": time.time() - 1}
        self.bot._note_activity()
        self.assertFalse(self.bot._provider_available(primary),
                         "群还在热聊，不该切回被让位的供应商")
        self.assertIn("缓存还热", self.bot._provider_block_reason(primary))

        # 群静下来超过缓存时效 → 放回来重试
        self.bot._activity["ts"] = time.time() - config.PROVIDER_CACHE_WARM_SECONDS - 1
        self.assertTrue(self.bot._provider_available(primary),
                        "群已经静过缓存时效了，该回去重试主供应商")

    def test_primary_comes_back_when_nobody_else_can_serve(self):
        """备用供应商也挂了的时候，必须让主供应商自己上 —— 否则整条链全哑。

        这条是上面那条的反例：缓存优先不能优先到「宁可不说话」。
        """
        if len(self.bot.PROVIDER_CHAIN) < 2:
            self.skipTest("未配置备用供应商")
        primary = config.AI_PROVIDER
        fallback = config.FALLBACK_PROVIDER
        self.bot._provider_state[primary] = {"fails": 0, "cooldown_until": time.time() - 1}
        self.bot._provider_state[fallback] = {"fails": 0, "cooldown_until": time.time() + 600}
        self.bot._note_activity()
        self.assertFalse(self.bot._provider_serving(fallback))
        self.assertTrue(self.bot._provider_available(primary),
                        "别家也服务不了时，被让位的那家必须能顶上来")

    def test_activity_stamp_moves_with_messages(self):
        """每收到一条群消息都要刷新时间戳 —— 它是「缓存还热不热」的唯一依据。"""
        self.bot._activity["ts"] = 0.0
        self.bot._note_activity()
        self.assertGreater(self.bot._activity["ts"], time.time() - 5)

    def test_hard_fail_marks_immediate(self):
        """连接层失败（代理断）一次就熔断，不凑满阈值。"""
        self.bot._provider_state.clear()
        self.bot._note_provider_fail("gemini", hard=True)
        st = self.bot._provider_state["gemini"]
        self.assertGreater(st["cooldown_until"], time.time())

    def test_soft_fail_needs_threshold(self):
        """普通错误要凑满阈值才熔断，避免偶发抖动就切换供应商。"""
        self.bot._provider_state.clear()
        for _ in range(config.PROVIDER_FAIL_THRESHOLD - 1):
            self.bot._note_provider_fail("gemini")
            self.assertLessEqual(
                self.bot._provider_state["gemini"]["cooldown_until"], time.time(),
                f"还没到 {config.PROVIDER_FAIL_THRESHOLD} 次，不该熔断")
        self.bot._note_provider_fail("gemini")
        self.assertGreater(self.bot._provider_state["gemini"]["cooldown_until"], time.time())

    def test_connection_errors_count_as_hard_fail(self):
        """连接层异常要判成硬失败 —— 守护的正是「代理断了」这个最典型的场景。

        坑在于：它不是靠错误消息里的关键词认出来的。代理一断，str(e) 只有一句
        "Connection error."，任何标记串都不出现；只有把异常类名一起看，才认得出
        这是「换模型、换 Key 都救不回来」。漏掉这步的后果是代理断了还要连撞满
        阈值才肯切供应商，用户白等好几轮。
        """
        import httpx
        from openai import APIConnectionError, APITimeoutError

        req = httpx.Request("POST", "https://example.invalid")
        for exc in (APIConnectionError(request=req), APITimeoutError(request=req)):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(self.bot._is_hard_fail(exc),
                                f"{type(exc).__name__} 应当判为硬失败")

    def test_transient_errors_are_not_hard_fail(self):
        """反例：偶发 5xx 与配额限流不该熔断，否则一次抖动就丢掉整家供应商。"""
        import httpx
        from openai import APIStatusError, RateLimitError

        req = httpx.Request("POST", "https://example.invalid")
        for exc in (
            RateLimitError("429 Too Many Requests",
                           response=httpx.Response(429, request=req), body=None),
            APIStatusError("internal server error",
                           response=httpx.Response(500, request=req), body=None),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(self.bot._is_hard_fail(exc),
                                 f"{type(exc).__name__} 不该判为硬失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
