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


class LastResortTest(unittest.TestCase):
    """全员**真熔断**（不是互让）时，谁都不出工 = 整条链哑掉。

    破锁解决不了这个：2026-09-18 14:18，智谱偶发一次 20s 超时被关 120s，而 Gemini
    正卡在地区不可用上 —— 于是整整两分钟机器人只会回「刚才走神了」。
    这时候沉默比多等一次更糟，得让最可能已经恢复的那家带伤上阵。
    """

    @classmethod
    def setUpClass(cls):
        import bot as _bot
        cls.bot = _bot

    def setUp(self):
        b = self.bot
        self.saved = (dict(b._provider_state), dict(b.AI_CLIENTS))
        now = time.time()
        b._provider_state.clear()
        # 两家都还在冷却：一个剩 97s，一个剩 80s
        b._provider_state["gemini"] = {"fails": 2, "cooldown_until": now + 97}
        b._provider_state["zhipu"] = {"fails": 2, "cooldown_until": now + 80}
        b.AI_CLIENTS.update({"gemini": ["fake"], "zhipu": ["fake"]})

    def tearDown(self):
        b = self.bot
        state, clients = self.saved
        b._provider_state.clear(); b._provider_state.update(state)
        b.AI_CLIENTS.clear(); b.AI_CLIENTS.update(clients)

    def test_picks_the_one_closest_to_recovering(self):
        chains = {"gemini": ["m1"], "zhipu": ["m2"]}
        self.assertEqual(self.bot._pick_last_resort(chains), "zhipu",
                         "该挑冷却剩余最短的那家，它最可能已经恢复")

    def test_providers_without_a_client_are_not_candidates(self):
        self.bot.AI_CLIENTS.pop("zhipu")
        self.assertEqual(self.bot._pick_last_resort({"gemini": ["m1"], "zhipu": ["m2"]}),
                         "gemini")

    def test_returns_none_when_nobody_is_configured(self):
        self.assertIsNone(self.bot._pick_last_resort({}))

    def test_nobody_is_available_yet_someone_still_answers(self):
        """端到端：全员熔断时，回复不该直接掉成兜底话术。"""
        import types

        b = self.bot
        saved_chain, saved_clients = list(b.PROVIDER_CHAIN), dict(b.AI_CLIENTS)
        b.PROVIDER_CHAIN[:] = ["gemini", "zhipu"]

        async def ok_create(*a, **kw):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="（带伤上阵）我还在。"))])

        b.AI_CLIENTS["zhipu"] = [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=ok_create)))]
        try:
            import asyncio
            got = asyncio.run(b.call_model([{"role": "user", "content": "在吗"}], 100))
        finally:
            b.PROVIDER_CHAIN[:] = saved_chain
            b.AI_CLIENTS.clear(); b.AI_CLIENTS.update(saved_clients)
        self.assertEqual(got, "（带伤上阵）我还在。",
                         "全员熔断时仍该试一次，而不是直接掉兜底话术")


class ProviderYieldDeadlockTest(unittest.TestCase):
    """「缓存优先」的自锁：A 看到 B 顶得上就让位，B 看到 A 顶得上也让位 —— 全哑。

    症状极具迷惑性：进程活着、消息收得到、也确实回了，只是回的全是「刚才走神了」
    这类兜底话术 —— **看起来就像宕机**。所以让位规则必须留一个破锁口子。
    """

    @classmethod
    def setUpClass(cls):
        import bot as _bot
        cls.bot = _bot

    def setUp(self):
        b = self.bot
        self.saved = (dict(b._provider_state), dict(b.AI_CLIENTS), b._activity["ts"])
        now = time.time()
        b._provider_state.clear()
        # 两家都「失败过但冷却已过」+ 都还有 client —— 互让死锁的必要条件
        for p in b.PROVIDER_CHAIN:
            b._provider_state[p] = {"fails": 2, "cooldown_until": now - 1}
        b.AI_CLIENTS.update({p: ["fake"] for p in b.PROVIDER_CHAIN})
        b._activity["ts"] = now          # 群刚刚还在聊 → 缓存还热

    def tearDown(self):
        b = self.bot
        state, clients, ts = self.saved
        b._provider_state.clear(); b._provider_state.update(state)
        b.AI_CLIENTS.clear(); b.AI_CLIENTS.update(clients)
        b._activity["ts"] = ts

    def test_mutual_yield_mutes_everyone(self):
        """前提：这个局面下两家确实都在让位。"""
        if len(self.bot.PROVIDER_CHAIN) < 2:
            self.skipTest("只配了一家供应商，构不成互让")
        avail = [p for p in self.bot.PROVIDER_CHAIN if self.bot._provider_available(p)]
        self.assertEqual(avail, [], "前提不成立：这个局面下本该全员让位")

    def test_turning_yield_off_breaks_the_deadlock(self):
        """关掉让位（只按熔断挑）后，必须至少有一家能出工。"""
        avail = [p for p in self.bot.PROVIDER_CHAIN
                 if self.bot._provider_available(p, allow_yield=False)]
        self.assertTrue(avail, "宁可多花一次 prefill，也不能不说话")

    def test_still_muted_while_actually_cooling_down(self):
        """破锁不能冲破熔断本身 —— 冷却期里谁都不该上。"""
        now = time.time()
        for p in self.bot.PROVIDER_CHAIN:
            self.bot._provider_state[p] = {"fails": 2, "cooldown_until": now + 60}
        avail = [p for p in self.bot.PROVIDER_CHAIN
                 if self.bot._provider_available(p, allow_yield=False)]
        self.assertEqual(avail, [], "熔断期内不该有人出工")


class StructuralFailureTest(unittest.TestCase):
    """地区封禁 / Key 失效这类「等一分钟也不会好」的错，不该每两分钟被放回来重撞一遍。

    起因是 2026-09-17~18 的日志复盘：Gemini 卡在 `User location is not supported`
    整整一天半，其间这类 400 撞了 **39 次** —— 每次冷却（120s）一到就被放回来重撞一轮，
    每轮还要顺着模型梯队连试 3 档；全员熔断时更离谱，它因为「冷却剩余最短」被挑去
    「带伤上阵」（14:53、14:56 两次），而它这一轮必挂。

    所以这类错要有自己的隔离策略：长得多的隔离窗 + 不许抢占带伤上阵的机会。
    """

    # 日志里 Gemini 返回的原文，一字不改 —— 用来钉死「认得出它」这件事
    GEMINI_REGION_ERR = ("Error code: 400 - [{'error': {'code': 400, 'message': "
                         "'User location is not supported for the API use.'")

    @classmethod
    def setUpClass(cls):
        import bot as _bot
        cls.bot = _bot

    def setUp(self):
        b = self.bot
        self.saved = (dict(b._provider_state), dict(b.AI_CLIENTS))
        b._provider_state.clear()
        b.AI_CLIENTS.update({"gemini": ["fake"], "zhipu": ["fake"]})

    def tearDown(self):
        b = self.bot
        state, clients = self.saved
        b._provider_state.clear(); b._provider_state.update(state)
        b.AI_CLIENTS.clear(); b.AI_CLIENTS.update(clients)

    def test_region_block_is_recognized(self):
        """正例：必须从真实错误串里认出结构性故障。"""
        self.assertTrue(self.bot._is_fatal_fail(RuntimeError(self.GEMINI_REGION_ERR)))

    def test_bad_credentials_are_recognized(self):
        """凭证失效同属这一类 —— Key 错了也不会因为等两分钟而变对。"""
        for msg in ("Error code: 401 - {'error': {'message': 'invalid api key'}}",
                    "Error code: 403 - permission denied for the api use"):
            with self.subTest(msg=msg[:40]):
                self.assertTrue(self.bot._is_fatal_fail(RuntimeError(msg)))

    def test_ordinary_outages_are_not_structural(self):
        """反例：抖动（超时/代理断/5xx/限流）还得留在短冷却那条路上，别被隔离半小时。"""
        import httpx
        from openai import APIConnectionError, APITimeoutError
        req = httpx.Request("POST", "https://example.invalid")
        for exc in (APIConnectionError(request=req), APITimeoutError(request=req),
                    RuntimeError("Error code: 500 - internal error"),
                    RuntimeError("Error code: 429 - RESOURCE_EXHAUSTED")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(self.bot._is_fatal_fail(exc),
                                 f"{type(exc).__name__} 只是抖动，不该按结构性故障隔离")

    def test_structural_failure_gets_the_long_isolation(self):
        """隔离时长要用长窗，而不是 PROVIDER_COOLDOWN_SECONDS。"""
        self.bot._note_provider_fail("gemini", fatal=True)
        st = self.bot._provider_state["gemini"]
        left = st["cooldown_until"] - time.time()
        self.assertGreater(
            left, config.PROVIDER_COOLDOWN_SECONDS,
            "结构性故障的隔离窗必须长于普通抖动，否则等于没改")
        self.assertLessEqual(left, config.PROVIDER_FATAL_COOLDOWN_SECONDS + 1,
                             "不该无限期拉黑，换代理/换 Key 后要能自愈")
        self.assertGreater(st["fatal_until"], time.time())

    def test_structural_failure_needs_no_threshold(self):
        """一次就够 —— 这类错不存在「凑几次再判定」，第一次就是结论。"""
        self.bot._note_provider_fail("gemini", fatal=True)
        self.assertGreater(self.bot._provider_state["gemini"]["cooldown_until"], time.time())

    def test_plain_outage_keeps_the_short_cooldown(self):
        """对照组：普通硬失败仍然走短冷却，别把改动扩散到别的场景。"""
        self.bot._note_provider_fail("gemini", hard=True)
        st = self.bot._provider_state["gemini"]
        self.assertLessEqual(st["cooldown_until"] - time.time(),
                             config.PROVIDER_COOLDOWN_SECONDS + 1)
        self.assertEqual(st.get("fatal_until", 0.0), 0.0)

    def test_blocks_reason_says_why(self):
        """日志要分得清「还在冷却」和「结构性出局」，否则下次排查还得翻源码。"""
        self.bot._note_provider_fail("gemini", fatal=True)
        self.assertIn("结构性故障", self.bot._provider_block_reason("gemini"))
        self.bot._provider_state["gemini"] = {
            "fails": 0, "cooldown_until": time.time() + 30, "fatal_until": 0.0}
        self.assertIn("熔断", self.bot._provider_block_reason("gemini"))
        self.assertNotIn("结构性故障", self.bot._provider_block_reason("gemini"))

    def test_structural_outage_loses_the_last_resort_slot(self):
        """它冷却剩得最短也不该被挑去带伤上阵 —— 那一轮必挂。"""
        now = time.time()
        self.bot._provider_state["gemini"] = {
            "fails": 0, "cooldown_until": now + 30, "fatal_until": now + 30}
        self.bot._provider_state["zhipu"] = {
            "fails": 0, "cooldown_until": now + 300, "fatal_until": 0.0}
        self.assertEqual(self.bot._pick_last_resort({"gemini": ["m"], "zhipu": ["m"]}),
                         "zhipu",
                         "结构性出局的那家必须让位给还有戏的那家")

    def test_everybody_structural_still_answers(self):
        """别无选择时仍然按老规矩挑剩余最短的 —— 沉默比多等一次更糟。"""
        now = time.time()
        self.bot._provider_state["gemini"] = {
            "fails": 0, "cooldown_until": now + 30, "fatal_until": now + 30}
        self.bot._provider_state["zhipu"] = {
            "fails": 0, "cooldown_until": now + 300, "fatal_until": now + 300}
        self.assertEqual(self.bot._pick_last_resort({"gemini": ["m"], "zhipu": ["m"]}),
                         "gemini")

    def test_recovery_clears_the_structural_flag(self):
        """应上答了就得把帽子摘掉，否则它恢复了却还排在最后。"""
        self.bot._note_provider_fail("gemini", fatal=True)
        self.bot._note_provider_ok("gemini")
        st = self.bot._provider_state["gemini"]
        self.assertEqual(st["cooldown_until"], 0.0)
        self.assertEqual(st["fatal_until"], 0.0)

    def test_structural_outage_is_not_called_while_isolated(self):
        """端到端：隔离期内这家**一次都不能被调用**，话由别家说。"""
        import asyncio
        import types

        b = self.bot
        b._note_provider_fail("gemini", fatal=True)

        calls = {"gemini": 0, "zhipu": 0}

        def make(who):
            async def create(*a, **kw):
                calls[who] += 1
                if who == "gemini":
                    raise RuntimeError(self.GEMINI_REGION_ERR)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content="我来答"))])
            return create

        b.AI_CLIENTS["gemini"] = [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=make("gemini"))))]
        b.AI_CLIENTS["zhipu"] = [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=make("zhipu"))))]

        saved_chain = list(b.PROVIDER_CHAIN)
        b.PROVIDER_CHAIN[:] = ["gemini", "zhipu"]
        try:
            got = asyncio.run(b.call_model([{"role": "user", "content": "在吗"}], 100))
        finally:
            b.PROVIDER_CHAIN[:] = saved_chain
        self.assertEqual(got, "我来答")
        self.assertEqual(calls["gemini"], 0,
                         "已知结构性出局的家庭还不重撞 —— 这正是这次修复的目的")


class SoloProviderLadderTest(unittest.IsolatedAsyncioTestCase):
    """只剩一家可用时，单个模型超时**不许把整家摁下去**。

    起因是 2026-09-18 16:11：Gemini 处于结构性隔离，智谱的 4.7 一次 20s 超时，
    代码按「硬失败」立刻把智谱整家关了 120s —— 梯队里剩的 4.5-air / 4-flash
    **连一次机会都没有**，用户当场吃到一句兜底话术。

    所以「踢多少」要看还有没有别人兜着：有人兜着就狠（一次即禁，别让用户每档都白等），
    没人兜着就得轻手（记一笔，凑够阈值才禁），让便宜的那几档有机会把话说完。
    """

    @classmethod
    def setUpClass(cls):
        import bot as _bot
        cls.bot = _bot

    def setUp(self):
        b = self.bot
        self.saved = (dict(b._provider_state), dict(b.AI_CLIENTS), list(b.PROVIDER_CHAIN))
        b._provider_state.clear()
        b._provider_state["gemini"] = {
            "fails": 0, "cooldown_until": time.time() + 900, "fatal_until": time.time() + 900}
        b.PROVIDER_CHAIN[:] = ["gemini", "zhipu"]

    def tearDown(self):
        b = self.bot
        state, clients, chain = self.saved
        b._provider_state.clear(); b._provider_state.update(state)
        b.AI_CLIENTS.clear(); b.AI_CLIENTS.update(clients)
        b.PROVIDER_CHAIN[:] = chain

    def _install(self, factory):
        import types
        self.bot.AI_CLIENTS.update({
            "gemini": [types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=factory("gemini"))))],
            "zhipu": [types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=factory("zhipu"))))],
        })

    def test_nobody_backing_up_is_detected(self):
        """前提判定：另一家处于隔离时，应当判定「没人兜底」。"""
        self.assertFalse(self.bot._someone_else_can_serve(
            "zhipu", {"gemini": ["m"], "zhipu": ["m"]}))

    def test_a_healthy_peer_counts_as_backup(self):
        """反例：别家能出工时，判定结果为「有人兜底」。"""
        self.bot._provider_state["gemini"] = {"fails": 0, "cooldown_until": 0.0}
        self.assertTrue(self.bot._someone_else_can_serve(
            "zhipu", {"gemini": ["m"], "zhipu": ["m"]}))

    async def test_weaker_sibling_gets_its_turn_when_nobody_else_serves(self):
        """端到端：最强那档超时后，梯队里剩下的那一档必须被叫到，而且要答得出来。"""
        import types

        tried = []

        async def create(who, model, *a, **kw):
            tried.append((who, model))
            if who == "zhipu" and model == "glm-4.7":
                raise RuntimeError("APITimeoutError: request timed out")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=f"{who}/{model} 应答"))])

        def factory(who):
            async def inner(*a, **kw):
                return await create(who, kw.get("model", "?"))
            return inner

        self._install(factory)
        saved = self.bot._TIER_CHAINS["chat"]
        self.bot._TIER_CHAINS["chat"] = {
            "gemini": ["gemini-dead"], "zhipu": ["glm-4.7", "glm-4.5-air"]}
        try:
            got = await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        finally:
            self.bot._TIER_CHAINS["chat"] = saved

        self.assertEqual(got, "zhipu/glm-4.5-air 应答",
                         "单档超时后，梯队里剩下的便宜档必须有机会把话说完")
        self.assertEqual([t for t in tried if t[0] == "zhipu"],
                         [("zhipu", "glm-4.7"), ("zhipu", "glm-4.5-air")])

    async def test_the_whole_provider_still_goes_down_after_enough_failures(self):
        """轻手不等于不设防：这家每一档都超时，最后仍要被熔断。"""
        import types

        async def create(*a, **kw):
            raise RuntimeError("APITimeoutError: request timed out")

        def factory(who):
            async def inner(*a, **kw):
                return await create(*a, **kw)
            return inner

        self._install(factory)
        saved = self.bot._TIER_CHAINS["chat"]
        self.bot._TIER_CHAINS["chat"] = {"gemini": ["gemini-dead"], "zhipu": ["m1", "m2", "m3"]}
        try:
            await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        finally:
            self.bot._TIER_CHAINS["chat"] = saved
        self.assertGreater(self.bot._provider_state["zhipu"]["cooldown_until"], time.time(),
                           "每一档都超时了，这家仍该被熔断 —— 阈值机制不能被绕过")

    async def test_structural_failures_are_never_downgraded(self):
        """地区封禁对这家所有模型一视同仁，不许为了「给它机会」放它继续撞。"""
        import types

        async def create(*a, **kw):
            raise RuntimeError(StructuralFailureTest.GEMINI_REGION_ERR)

        self.bot._provider_state.pop("gemini")  # 本次自己撞出隔离，不预设
        self.bot._provider_state.pop("zhipu", None)
        self.bot.AI_CLIENTS.update({"gemini": [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))]})
        self.bot.AI_CLIENTS.pop("zhipu", None)
        saved = self.bot._TIER_CHAINS["chat"]
        self.bot._TIER_CHAINS["chat"] = {"gemini": ["g1", "g2", "g3"]}
        try:
            await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        finally:
            self.bot._TIER_CHAINS["chat"] = saved
        left = self.bot._provider_state["gemini"]["cooldown_until"] - time.time()
        self.assertGreater(left, config.PROVIDER_COOLDOWN_SECONDS,
                           "结构性故障必须立刻进长隔离窗，不能因为没人兜底就降级成普通失败")


class SlowModelTest(unittest.IsolatedAsyncioTestCase):
    """超时是**那一档自己的事**，不许它占着队首让每一轮都白等一个满超时。

    起因是 2026-09-18 晚用户的「又开始丢响应了」：glm-4.7 一小时里超时 4 次，每次
    第一步就吃掉 20s，而整轮预算 `AI_TOTAL_TIMEOUT` 只有 30s —— 剩下 10s 常常连第二档
    都跑不完，于是用户连着吃到「刚才走神了」；更糟的是超时还会把整家供应商送进 120s
    冷却，把后面几句一起赔进去。

    所以超时除了「去找下一档」，还要给这一档挂免战牌（按时过期，不是永久降级）。
    """

    @classmethod
    def setUpClass(cls):
        import bot as _bot
        cls.bot = _bot

    def setUp(self):
        b = self.bot
        self.saved = (dict(b._provider_state), dict(b._slow_until), dict(b.AI_CLIENTS),
                      list(b.PROVIDER_CHAIN), b._TIER_CHAINS["chat"])
        b._provider_state.clear()
        b._slow_until.clear()
        # Gemini 处于结构性隔离 ⇒ 智谱是唯一指望，正好触发「没人兜底」的轻手路径
        b._provider_state["gemini"] = {
            "fails": 0, "cooldown_until": time.time() + 900, "fatal_until": time.time() + 900}
        b.PROVIDER_CHAIN[:] = ["gemini", "zhipu"]

    def tearDown(self):
        b = self.bot
        state, slow, clients, chain, tiers = self.saved
        b._provider_state.clear(); b._provider_state.update(state)
        b._slow_until.clear(); b._slow_until.update(slow)
        b.AI_CLIENTS.clear(); b.AI_CLIENTS.update(clients)
        b.PROVIDER_CHAIN[:] = chain
        b._TIER_CHAINS["chat"] = tiers

    def _install(self, answer_map):
        import types

        def factory(who):
            async def create(*a, **kw):
                outcome = answer_map.get((who, kw.get("model")))
                if isinstance(outcome, Exception):
                    raise outcome
                if outcome == "timeout":
                    raise asyncio.TimeoutError()
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=outcome or f"{who}/{kw.get('model')}"))])
            return create

        self.bot.AI_CLIENTS.update({"gemini": [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=factory("gemini"))))],
            "zhipu": [types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=factory("zhipu"))))]})

    async def test_slow_tier_gets_skipped_next_time(self):
        """核心诉求：上一轮超时过的那一档，下一轮不许再占队首。"""
        tried = []

        def record(who, model):
            tried.append(model)

        self._install({("zhipu", "glm-4.7"): "timeout" if len(tried) < 1 else None})
        # 第一次撞超时（只撞一次），之后全部正常
        calls = {"n": 0}

        import types

        def factory(who):
            async def create(*a, **kw):
                model = kw.get("model")
                tried.append(model)
                calls["n"] += 1
                if model == "glm-4.7" and calls["n"] == 1:
                    raise asyncio.TimeoutError()
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=f"{model} 应答"))])
            return create

        self.bot.AI_CLIENTS.clear()
        self.bot.AI_CLIENTS.update({"gemini": [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=factory("gemini"))))],
            "zhipu": [types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=factory("zhipu"))))]})
        self.bot._TIER_CHAINS["chat"] = {"gemini": ["g"], "zhipu": ["glm-4.7", "glm-4.5-air"]}

        await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        self.assertFalse(self.bot._provider_state.get("zhipu", {}).get("cooldown_until"),
                         "没人兜底时，单档超时不该把整家拉黑")
        self.assertTrue(self.bot._model_slow("zhipu", "glm-4.7"), "超时过的那一档该挂免战牌")

        tried.clear()
        got = await self.bot.call_model([{"role": "user", "content": "再一句"}], 100)
        self.assertEqual(got, "glm-4.5-air 应答",
                         "下一轮必须直接由没超时的那一档接住，而不是再撞一次慢档")
        self.assertNotIn("glm-4.7", tried, "惩罚期内不该再叫它")

    async def test_penalty_expires_and_the_model_comes_back(self):
        """免战牌是按时过期的，不是永久降级 —— 凉够了自己回原位试运行。"""
        self.bot._slow_until[("zhipu", "glm-4.7")] = time.time() - 1
        self.assertFalse(self.bot._model_slow("zhipu", "glm-4.7"))

        import types
        self._install({("zhipu", "glm-4.7"): "4.7 又行了"})
        self.bot._TIER_CHAINS["chat"] = {"gemini": ["g"], "zhipu": ["glm-4.7", "glm-4.5-air"]}
        got = await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        self.assertEqual(got, "4.7 又行了", "过期后要给它机会，答得上来就继续用")

    async def test_all_tiers_penalized_still_tries_somebody(self):
        """全都被罚时不能硬躲 —— 躲到最后一个人都不出工，等于自己饿死。"""
        import types
        now = time.time() + 300
        for m in ("glm-4.7", "glm-4.5-air"):
            self.bot._slow_until[("zhipu", m)] = now
        self._install({("zhipu", "glm-4.7"): "还是我接"})
        self.bot._TIER_CHAINS["chat"] = {"gemini": ["g"], "zhipu": ["glm-4.7", "glm-4.5-air"]}
        got = await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        self.assertEqual(got, "还是我接", "全罚了也要照常试，不能返回 None")

    async def test_provider_still_benched_when_others_can_serve(self):
        """对照组：有别家兜着时保持老规矩 —— 一次超时就把这家摁下去。"""
        import types

        def factory(who):
            async def create(*a, **kw):
                if who == "zhipu":
                    raise asyncio.TimeoutError()
                return types.SimpleNamespace(choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content="gemini 顶上了"))])
            return create

        self.bot._provider_state.pop("gemini")  # 让 Gemini 可用
        # 把智谱摆到队首，否则 Gemini 先答上来就走不到超时那段
        self.bot.PROVIDER_CHAIN[:] = ["zhipu", "gemini"]
        self.bot.AI_CLIENTS.clear()
        self.bot.AI_CLIENTS.update({"gemini": [types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=factory("gemini"))))],
            "zhipu": [types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=factory("zhipu"))))]})
        self.bot._TIER_CHAINS["chat"] = {"gemini": ["g"], "zhipu": ["glm-4.7"]}
        await self.bot.call_model([{"role": "user", "content": "在吗"}], 100)
        self.assertGreater(self.bot._provider_state["zhipu"]["cooldown_until"], time.time(),
                           "有人兜底时维持一次即拉黑，别让用户陪它每一档都等一轮")


if __name__ == "__main__":
    unittest.main(verbosity=2)
