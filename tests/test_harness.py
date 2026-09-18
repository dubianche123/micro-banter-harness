"""极简 harness 回归测试：提示词前缀顺序 + 压缩结算好感度。

真正要钉住的不变量只有一条：
    **system 块里不许出现随「谁在说 / 说到第几句」变化的内容。**

破了这条不会有任何报错，只会让前缀缓存从那一句起全部作废 —— 悄悄变慢变贵，
所以必须靠测试守住。缓存命中率实测：重排前 85%，重排后 98%。
"""
import inspect
import sys
import time
import unittest

sys.path.insert(0, "..")

import config  # noqa: E402
import digest  # noqa: E402
import prompts  # noqa: E402

ENVELOPE_MARK = "（以下是系统给你的即时提示"
# 插嘴指令的锚点从提示词模块取，不在测试里抄一份文案 —— 改措辞不该让这条测试挂掉
BANTER_MARK = prompts.BANTER_MARK


class HarnessOrderTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        global bot, prompts
        import bot as _bot
        import prompts as _prompts
        cls.bot = _bot
        cls.prompts = _prompts

    def setUp(self):
        self.captured = {}
        self._orig_call = self.bot.call_model

        async def fake_call(messages, max_tokens, tier="chat"):
            self.captured["messages"] = messages
            self.captured["max_tokens"] = max_tokens
            self.captured["tier"] = tier
            return "行，记下了。"

        self.bot.call_model = fake_call

    def tearDown(self):
        self.bot.call_model = self._orig_call

    async def grab(self, session_id, user_text="在吗", **kw):
        await self.bot.get_ai_reply(session_id, user_text, **kw)
        return self.captured["messages"]

    # ── 顺序 ──

    async def test_block_order_is_stable_first(self):
        """稳定头 → 每日记忆 → 历史 → 本轮动态，顺序不能乱。"""
        msgs = await self.grab(
            "th_order_1", "老张今天来了吗",
            is_owner=True, mode="normal",
            context_hint="【群聊最近背景】- 阿强：今天天气不错",
            relation_note="【你对这位群友的记忆】很熟",
            group_memory="【这个群最近发生的事】上周在聊黑猴",
        )
        self.assertEqual([m["role"] for m in msgs], ["system", "system", "user"])
        self.assertIn("上周在聊黑猴", msgs[1]["content"])
        # 群长期记忆是「一天才变一次」的，必须紧跟稳定头、排在历史之前
        self.assertNotIn("今天天气不错", msgs[1]["content"])

    async def test_volatile_content_only_in_tail(self):
        """群主身份、群聊背景、关系档案、插嘴指令都只能在最后那条 user 里。"""
        msgs = await self.grab(
            "th_order_2", "老张今天来了吗",
            is_owner=True, is_random_banter=True,
            context_hint="- 阿强：今天天气不错",
            relation_note="【你对这位群友的记忆】很熟",
        )
        # 认这两个块只看锚点，不绑死整段文案 —— 改措辞不该让这条测试挂掉
        owner_mark = self.prompts.PROMPT_OWNER_NOTE.strip()[:12]
        banter_mark = BANTER_MARK
        head = "\n".join(m["content"] for m in msgs[:-1])
        for piece in ("【群聊最近背景】", "阿强：今天天气不错", "很熟",
                      owner_mark, banter_mark):
            self.assertNotIn(piece, head, f"易变内容 {piece!r} 混进了稳定块")

        tail = msgs[-1]["content"]
        self.assertTrue(tail.startswith(ENVELOPE_MARK))
        for piece in ("阿强：今天天气不错", "很熟", banter_mark):
            self.assertIn(piece, tail)
        self.assertTrue(tail.rstrip().endswith("老张今天来了吗"))

    async def test_output_rules_sit_in_stable_head(self):
        """排版约束要在稳定头里，对所有模式生效。

        它防的是「换个供应商就带 Markdown」：同一个请求，Gemini 会输出
        「那必须是**某位群友请客**啊！」，星号原样发进 QQ 群只会显示成符号；智谱则不会。
        所以要跟模型无关地钉在稳定头里，而不是塞进某一次的即时指令。
        """
        for mode in ("normal", "catgirl"):
            msgs = await self.grab("th_out_%s" % mode, "在吗", mode=mode)
            self.assertIn(self.prompts.PROMPT_OUTPUT_RULES, msgs[0]["content"],
                          f"{mode} 模式的稳定头没带上排版约束")

        # 稳定头每轮必须是同一份，否则前缀缓存整段作废
        again = await self.grab("th_out_normal2", "吃饭了吗", mode="normal")
        first = await self.grab("th_out_normal", "在吗", mode="normal")
        self.assertEqual(again[0]["content"], first[0]["content"])

    async def test_shared_rules_sit_in_stable_head(self):
        """防幻觉的身份/关系硬规则要在稳定头里，对所有模式生效。

        它防的是「机器人自己编关系」：实测群友拿「我是你爹」开涮，机器人被问
        「我是谁」时顺着编了一段辈分关系。关系只能来自系统档案，所以这条跟人设无关，
        必须钉在稳定头里，而不是某个模式的私货。
        """
        for mode in ("normal", "catgirl", "crazy"):
            msgs = await self.grab("th_rules_%s" % mode, "在吗", mode=mode)
            self.assertIn(self.prompts.PROMPT_SHARED_RULES, msgs[0]["content"],
                          f"{mode} 模式的稳定头没带上身份/关系硬规则")

    # ── 缓存命中的核心断言 ──

    async def test_head_identical_across_speakers(self):
        """群主和普通群友各触发一次，稳定头必须逐字节相同 —— 缓存能否共享全看这条。"""
        m_owner = await self.grab("th_spk_owner", "来了", is_owner=True,
                                  context_hint="- 阿强：早", relation_note="【记忆】群主档案")
        m_member = await self.grab("th_spk_member", "早", is_owner=False,
                                   context_hint="- 阿远：早上好", relation_note="【记忆】阿远档案")
        self.assertEqual(m_owner[0]["content"], m_member[0]["content"])

    async def test_head_stable_across_turns(self):
        """同一个人的连续两轮，稳定头也不能变（否则每轮都要重新 prefill）。"""
        sid = "th_turn_x"
        first = await self.grab(sid, "第一句", relation_note="【记忆】甲",
                                context_hint="- 甲：一")
        second = await self.grab(sid, "第二句", relation_note="【记忆】甲",
                                 context_hint="- 甲：二")
        self.assertEqual(first[0]["content"], second[0]["content"])

    # ── 协议与历史 ──

    async def test_cmd_protocol_off_by_default(self):
        """默认不再逼模型吐 <CMD> 记账行 —— 好感度改由压缩结算。"""
        msgs = await self.grab("th_cmd_1", "你好", is_owner=True, mode="normal",
                               relation_note="【记忆】x")
        blob = "\n".join(m["content"] for m in msgs)
        self.assertFalse(config.CMD_PROTOCOL_ENABLED)
        self.assertNotIn("<CMD>", blob)

    async def test_scoring_rubric_travels_with_protocol(self):
        """「记账口径」是协议的一部分，协议关了它也不该占稳定头的位置。"""
        msgs = await self.grab("th_hint_1", "喵", mode="catgirl",
                               relation_note="【记忆】x")
        self.assertNotIn("记账口径", msgs[0]["content"])

    async def test_envelope_never_stored_in_history(self):
        """信封只贴在发出去的请求上，历史里存的是原话，否则会一轮轮堆成雪球。"""
        sid = "th_hist_1"
        await self.grab(sid, "第一句", context_hint="- 甲：背景一句话",
                        relation_note="【记忆】甲")
        msgs = await self.grab(sid, "第二句", context_hint="- 甲：又一句",
                               relation_note="【记忆】甲")
        body = [m for m in msgs if m["role"] == "user"]
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["content"], "第一句")
        self.assertNotIn(ENVELOPE_MARK, body[0]["content"])
        self.assertTrue(body[1]["content"].startswith(ENVELOPE_MARK))
        self.assertTrue(body[1]["content"].rstrip().endswith("第二句"))


    async def test_chat_uses_chat_tier(self):
        """对话一律走对话梯队。"""
        await self.grab("th_tier_chat", "帮我看看这个", relation_note="【记忆】甲")
        self.assertEqual(self.captured["tier"], "chat")

    async def test_banter_shares_the_same_tier(self):
        """插嘴必须和正经对话同一条梯队。

        按「插嘴/正经」切会让同一个角色忽聪明忽笨；而且实测插嘴一天 0 次
        （归档消息长度中位数 7 字，够不着 10 字门槛），为它单独换模型纯属白搭。
        """
        await self.grab("th_tier_banter", "哈哈", is_random_banter=True,
                        relation_note="【记忆】甲")
        self.assertEqual(self.captured["tier"], "chat")

    async def test_ask_digest_uses_digest_tier(self):
        """压缩走压缩梯队：与聊天不共享前缀，换模型零缓存损失。"""
        await self.bot.ask_digest("sys", "user")
        self.assertEqual(self.captured["tier"], "digest")


class ModelTierTest(unittest.TestCase):
    """梯队分工切在「任务」上：对话走对话梯队，压缩走压缩梯队。"""

    @classmethod
    def setUpClass(cls):
        global bot
        import bot as _bot
        cls.bot = _bot

    def test_chat_chain_puts_quality_first(self):
        self.assertEqual(self.bot.MODEL_CHAINS["zhipu"][0], "glm-4.7")

    def test_digest_chain_is_cheap(self):
        chain = self.bot.MODEL_CHAINS_DIGEST["zhipu"]
        self.assertEqual(chain[0], "glm-4.5-air")

    def test_digest_chain_never_falls_back_to_chat_models(self):
        """两个压缩模型都挂了就该等下一轮，不能悄悄退到对话梯队去烧 4.7 的额度。"""
        chain = self.bot.MODEL_CHAINS_DIGEST["zhipu"]
        self.assertNotIn("glm-4.7", chain)

    def test_digest_chain_follows_preset_or_falls_back(self):
        """digest 梯队 = 配了就用配的，没配就整条照抄对话梯队（不能是空的）。

        这条以前是拿 Gemini 当样本写死的（它当时没配 models_digest）。后来给 Gemini
        也补上压缩梯队，断言就跟着过时了 —— 规则稳定，样本会变，所以直接测规则。
        """
        for name, chain in self.bot.MODEL_CHAINS_DIGEST.items():
            preset = config.PROVIDER_PRESETS[name]
            expected = list(preset.get("models_digest") or preset["models"])
            self.assertEqual(chain, expected, name)
            self.assertTrue(chain, "%s 的 digest 梯队不能为空" % name)

    def test_judge_chain_follows_preset_or_falls_back(self):
        """judge 梯队同理：审核这种活宁可只留一档，也不能静默退到弱档去。"""
        for name, chain in self.bot.MODEL_CHAINS_JUDGE.items():
            preset = config.PROVIDER_PRESETS[name]
            expected = list(preset.get("models_judge") or preset["models"])
            self.assertEqual(chain, expected, name)
            self.assertTrue(chain, "%s 的 judge 梯队不能为空" % name)


class DigestTriggerTest(unittest.TestCase):
    """压缩触发：攒够条数就压；冷清的群每天兜底一次。"""

    def setUp(self):
        self.store = digest.GroupDigest()
        self.store.groups["G"] = {"summary": None, "last_run": time.time()}

    def test_count_trigger_fires_without_waiting(self):
        self.assertTrue(self.store.needs_compress(
            "G", interval_hours=24.0, min_entries=15,
            pending_count=120, count_trigger=120))

    def test_below_count_still_waits_for_interval(self):
        self.assertFalse(self.store.needs_compress(
            "G", 24.0, 15, pending_count=50, count_trigger=120))

    def test_daily_fallback_fires_for_quiet_group(self):
        self.store.groups["G"]["last_run"] = time.time() - 25 * 3600
        self.assertTrue(self.store.needs_compress(
            "G", 24.0, 15, pending_count=20, count_trigger=120))

    def test_too_few_messages_never_fires(self):
        self.store.groups["G"]["last_run"] = time.time() - 100 * 3600
        self.assertFalse(self.store.needs_compress(
            "G", 24.0, 15, pending_count=3, count_trigger=120))

    def test_full_queue_fires_immediately(self):
        self.assertTrue(self.store.needs_compress(
            "G", 24.0, 15, pending_count=self.store.max_entries + 1, count_trigger=120))

    def test_reason_says_why(self):
        self.assertIn("攒够", self.store.compress_reason("G", 24.0, 200, count_trigger=120))
        self.assertIn("每日兜底", self.store.compress_reason("G", 24.0, 20, count_trigger=120))


class BuildMessagesTest(unittest.TestCase):
    """storage 层的拼装规则，直接测，不绕模型。"""

    @classmethod
    def setUpClass(cls):
        global bot
        import bot as _bot
        cls.bot = _bot

    def test_blocks_land_in_place(self):
        msgs = self.bot.SESSIONS.build_messages(
            "th_build_1", "HEAD", "原话", memory_block="MEM", turn_context="CTX")
        self.assertEqual([m["role"] for m in msgs], ["system", "system", "user"])
        self.assertEqual(msgs[0]["content"], "HEAD")
        self.assertEqual(msgs[1]["content"], "MEM")
        self.assertEqual(msgs[2]["content"], "CTX\n\n原话")

    def test_optional_blocks_omitted_when_empty(self):
        msgs = self.bot.SESSIONS.build_messages("th_build_2", "HEAD", "原话")
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertEqual(msgs[1]["content"], "原话")


class DigestAffinityTest(unittest.TestCase):
    """摘要要把 affinity 传下来，脏数据不许进档案。"""

    def test_normalize_keeps_affinity(self):
        out = digest.normalize({"topics": [], "affinity": [{"who": "阿强", "delta": 2}]})
        self.assertEqual(len(out["affinity"]), 1)
        for key in ("topics", "memes", "promises", "people", "affinity"):
            self.assertIn(key, out)

    def test_normalize_drops_non_dict_rows(self):
        out = digest.normalize({"affinity": ["阿强", 3, None, {"who": "乙", "delta": 1}]})
        self.assertEqual(out["affinity"], [{"who": "乙", "delta": 1}])

    def test_affinity_not_fed_back_to_model(self):
        """已结算过的好感度不能回喂，否则同一批互动会被反复加减分。"""
        store = digest.GroupDigest()
        slot = {"summary": {"brief": "简报", "data": {"affinity": [{"who": "阿强", "delta": 3}]}}}
        self.assertNotIn("阿强", store.old_summary_text(slot))


class ApplyDigestAffinityTest(unittest.TestCase):
    """压缩结算落库：昵称要能反查回 openid，幅度要夹住，别虚增搭话次数。"""

    GID = "TESTGID_CACHE_HARNESS"

    @classmethod
    def setUpClass(cls):
        global bot, relations
        import bot as _bot
        import relations as _relations
        cls.bot = _bot
        cls.relations = _relations

    def setUp(self):
        self.bot.RELATIONS.set_nick(self.GID, "OPENID_STRONG", "阿强")
        self.bot.RELATIONS.set_nick(self.GID, "OPENID_QUIET", "阿远")

    def tearDown(self):
        prefix = f"{self.GID}|"
        for k in [k for k in list(self.bot.RELATIONS.records) if k.startswith(prefix)]:
            self.bot.RELATIONS.records.pop(k, None)

    def test_maps_nick_and_clamps_span(self):
        n, detail = self.bot.apply_digest_affinity(self.GID, [
            {"who": "阿强", "delta": 9},        # 超过日上限，应被夹到 +6
            {"who": "阿远", "delta": -2},
            {"who": "查无此人", "delta": 5},     # 名字对不上：宁可不记，也不能记错人
        ])
        self.assertEqual(n, 2)
        strong = self.bot.RELATIONS.get(self.GID, "OPENID_STRONG", create=False)
        quiet = self.bot.RELATIONS.get(self.GID, "OPENID_QUIET", create=False)
        self.assertEqual(strong["score"], config.AFFINITY_DIGEST_SPAN)
        self.assertEqual(quiet["score"], -2)
        self.assertIn("阿强", detail)

    def test_does_not_inflate_interactions(self):
        before = self.bot.RELATIONS.get(self.GID, "OPENID_STRONG")["interactions"]
        self.bot.apply_digest_affinity(self.GID, [{"who": "阿强", "delta": 2}])
        after = self.bot.RELATIONS.get(self.GID, "OPENID_STRONG", create=False)
        self.assertEqual(after["interactions"], before)

    def test_bad_rows_are_skipped(self):
        n, _ = self.bot.apply_digest_affinity(self.GID, [
            {"who": "阿强", "delta": "不是数字"},
            {"who": "阿强"},
            "阿强",
            None,
            {"who": "阿强", "delta": 0},        # 没变化就别写档案
        ])
        self.assertEqual(n, 0)

    def test_disabled_flag_is_a_noop(self):
        n, detail = self.bot.apply_digest_affinity(self.GID, [])
        self.assertEqual((n, detail), (0, ""))


class CompressAffinityIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """端到端：压缩产出 affinity → 落进关系档案（不打网络，用假模型）。"""

    GID = "TESTGID_COMPRESS_AFF"

    @classmethod
    def setUpClass(cls):
        global bot
        import bot as _bot
        cls.bot = _bot

    def tearDown(self):
        prefix = f"{self.GID}|"
        for k in [k for k in list(self.bot.RELATIONS.records) if k.startswith(prefix)]:
            self.bot.RELATIONS.records.pop(k, None)

    async def test_compress_then_land_in_store(self):
        self.bot.RELATIONS.set_nick(self.GID, "OPENID_STRONG", "阿强")

        raw = ('这阵子群里天天在聊黑猴。\n'
               '<DIGEST>{"affinity":[{"who":"阿强","delta":2,"why":"一直在接梗"}],'
               '"topics":[]}</DIGEST>')

        async def ask(system, user):
            return raw

        store = digest.GroupDigest()
        entries = [{"sender": "OPENID_STRONG", "text": "机器人在吗", "ts": time.time()}]
        out = await digest.compress_group(store, self.GID, entries, ask)

        self.assertIsNotNone(out)
        self.assertIn("黑猴", out["brief"])
        self.assertEqual(out["data"]["affinity"][0]["who"], "阿强")

        n, _ = self.bot.apply_digest_affinity(self.GID, out["data"]["affinity"])
        self.assertEqual(n, 1)
        rec = self.bot.RELATIONS.get(self.GID, "OPENID_STRONG", create=False)
        self.assertEqual(rec["score"], 2)

    async def test_compress_survives_missing_affinity(self):
        """模型没给 affinity（老格式）也不能炸，其他字段照常入库。"""
        async def ask(system, user):
            return '简报正文\n<DIGEST>{"topics":[{"topic":"黑猴","heat":3}]}</DIGEST>'

        store = digest.GroupDigest()
        entries = [{"sender": "OPENID_X", "text": "聊聊", "ts": time.time()}]
        out = await digest.compress_group(store, self.GID, entries, ask)
        self.assertEqual(out["data"]["affinity"], [])
        n, _ = self.bot.apply_digest_affinity(self.GID, out["data"]["affinity"])
        self.assertEqual(n, 0)


class NameRefreshTest(unittest.TestCase):
    """喂给模型的文本里，一个人只许有**当前**这一个称呼。

    旧名一旦漏进 prompt，模型就会拿它叫人，或者把旧名和新名当成两个人开始编往事
    （「明明改了名它还在叫」「容易弄混」的根因就在这）。
    """

    GID = "TESTGID_NAME_REFRESH"
    OPENID = "OPENID_RENAME_CASE"
    OTHER = "OPENID_RENAME_OTHER"

    @classmethod
    def setUpClass(cls):
        global bot
        import bot as _bot
        cls.bot = _bot

    def setUp(self):
        self.bot.RELATIONS.set_nick(self.GID, self.OPENID, "小满")
        self.bot.RENAMES.note(self.GID, "阿龙", self.OPENID)

    def tearDown(self):
        for oid in (self.OPENID, self.OTHER):
            self.bot.RELATIONS.records.pop(f"{self.GID}|{oid}", None)
        self.bot.RENAMES.groups.pop(self.GID, None)

    def test_old_name_is_translated_to_current(self):
        out = self.bot.refresh_names(self.GID, "阿龙昨天迟到，阿龙还欠我一杯奶茶")
        self.assertNotIn("阿龙", out)
        self.assertEqual(out.count("小满"), 2)

    def test_revoked_name_becomes_placeholder(self):
        self.bot.RELATIONS.set_nick(self.GID, self.OPENID, None)
        out = self.bot.refresh_names(self.GID, "阿龙来了")
        self.assertNotIn("阿龙", out)
        self.assertIn("未留名", out)

    def test_name_now_held_by_someone_else_is_left_alone(self):
        self.bot.RELATIONS.set_nick(self.GID, self.OTHER, "阿龙")
        self.assertEqual(self.bot.refresh_names(self.GID, "阿龙在吗"), "阿龙在吗")

    def test_text_without_old_names_is_untouched(self):
        text = "【群史记】这阵子群里安静得离谱"
        self.assertEqual(self.bot.refresh_names(self.GID, text), text)


class PrimaryNameTest(unittest.TestCase):
    """称呼分主副：主名只认「本人认领」和「群主授权」两个来源。

    副名（改名前留下的旧叫法）归台账管，它只能把旧字面改写成**当前**称呼，
    反过来一个字都动不了主名。

    这条不变量以前只是「碰巧没有别的调用点」—— 没有任何东西拦着。真实风险是
    「我自己设的名字被总结出来的名字顶掉了」：一旦发生，当事人立刻会觉得这机器人
    不可信，比「没记住名字」严重得多。所以要用测试钉住，而不是靠没写别的调用点。
    """

    GID = "TESTGID_PRIMARY_NAME"
    OTHER_GID = "TESTGID_PRIMARY_NAME_B"
    A = "OPENID_PRIMARY_A"
    B = "OPENID_PRIMARY_B"

    @classmethod
    def setUpClass(cls):
        global bot
        import bot as _bot
        cls.bot = _bot

    def tearDown(self):
        for gid in (self.GID, self.OTHER_GID):
            prefix = f"{gid}|"
            for k in [k for k in list(self.bot.RELATIONS.records) if k.startswith(prefix)]:
                self.bot.RELATIONS.records.pop(k, None)
            self.bot.RENAMES.groups.pop(gid, None)

    # ── 写权限：只有两个来源 ──

    def test_claim_and_owner_are_the_only_writers(self):
        self.assertEqual(
            self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim"), "阿强")
        self.assertEqual(
            self.bot.RELATIONS.set_nick(self.GID, self.B, "阿远", source="owner"), "阿远")

    def test_any_other_source_is_refused(self):
        """摘要、模型推断、任何自动同步来的名字，一个都写不进来。"""
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim")
        for bad in ("digest", "model", "summary", "auto", "guess", "", None):
            with self.subTest(source=bad):
                self.assertIsNone(
                    self.bot.RELATIONS.set_nick(self.GID, self.A, "阿龙", source=bad))
                rec = self.bot.RELATIONS.get(self.GID, self.A, create=False)
                self.assertEqual(rec["nick"], "阿强")

    def test_refusal_does_not_even_clear(self):
        """挡就挡干净：非法来源连「清空」都做不到。

        留个「不能改、但能被抹掉」的半开门没有意义 —— 抹掉之后模型再顺手补一个，
        等于绕过去了。
        """
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim")
        self.assertIsNone(
            self.bot.RELATIONS.set_nick(self.GID, self.A, None, source="digest"))
        self.assertEqual(
            self.bot.RELATIONS.get(self.GID, self.A, create=False)["nick"], "阿强")

    def test_refusal_leaves_no_empty_record(self):
        """守卫在取档案之前就返回，不该顺手建出一条空记录。"""
        self.bot.RELATIONS.set_nick(self.GID, "OPENID_NEVER_SEEN", "阿龙", source="digest")
        self.assertIsNone(
            self.bot.RELATIONS.get(self.GID, "OPENID_NEVER_SEEN", create=False))

    # ── 豁免名单：主名不许被自动逻辑改写 ──

    def test_main_names_are_scoped_per_group(self):
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim")
        self.bot.RELATIONS.set_nick(self.OTHER_GID, self.B, "阿远", source="claim")
        self.assertEqual(self.bot.RELATIONS.main_names(self.GID), {"阿强"})
        self.assertEqual(self.bot.RELATIONS.main_names(self.OTHER_GID), {"阿远"})

    def test_refresh_leaves_a_name_held_by_someone_else(self):
        """台账里「阿龙」记的是 A，可 B 现在正拿它当主名 —— 一个字都不许改。"""
        self.bot.RELATIONS.set_nick(self.GID, self.A, "小满", source="claim")
        self.bot.RENAMES.note(self.GID, "阿龙", self.A)
        self.bot.RELATIONS.set_nick(self.GID, self.B, "阿龙", source="claim")
        self.assertEqual(self.bot.refresh_names(self.GID, "阿龙在吗"), "阿龙在吗")

    def test_sync_rename_spares_a_name_held_by_someone_else(self):
        """A 改名了，但旧名现在归 B —— 会话和长期记忆里那些字面不能跟着改。

        调用顺序就是真实顺序：set_nick 先跑（A 已经叫新名了），再同步历史文本。
        此刻旧名还在主名名单里，只可能是别人正用着。
        """
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿远", source="claim")
        self.bot.RELATIONS.set_nick(self.GID, self.B, "阿强", source="claim")
        self.assertFalse(self.bot._sync_rename(self.GID, "阿强", "阿远"))

    def test_sync_clear_spares_a_name_held_by_someone_else(self):
        """A 撤销了自己的称呼，B 还叫这个 —— 撤销的是「我不用了」，不是名字作废。"""
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim")
        self.bot.RELATIONS.set_nick(self.GID, self.B, "阿强", source="claim")
        self.assertFalse(self.bot._sync_clear(self.GID, self.A, "阿强"))

    # ── 模型那条路：只读，不回写 ──

    def test_digest_rows_never_write_a_name(self):
        """摘要里的名字只用来反查 openid，永远不回写主名。

        这是「模型总结出来的名字把主名顶掉」唯一可能存在的那条路，单独钉一道。
        """
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim")
        self.bot.apply_digest_affinity(
            self.GID, [{"who": "阿强", "delta": 2}, {"who": "阿龙", "delta": 3}])
        self.assertEqual(
            self.bot.RELATIONS.get(self.GID, self.A, create=False)["nick"], "阿强")
        # 摘要里冒出来的「阿龙」没人认领过：不建档案，也不会变成谁的称呼
        self.assertEqual(self.bot.RELATIONS.main_names(self.GID), {"阿强"})


class NickCollisionEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """端到端：重名真的被挡在写入之前，而不只是判断函数自己正确。

    单元测试盯的是判断逻辑（`test_nick.NameCollisionTest`），这条盯的是**调用点接上了** ——
    这个项目吃过「逻辑写了、没接到调用点」的亏（`MAIN_NAME_SOURCES` 那次就是），
    所以两条都要有。同时钉住「随时可以覆盖自己的名字」不被这条规则误伤。
    """

    GID = "TESTGID_NICK_COLLISION"
    OWNER = "OPENID_NC_OWNER"
    A = "OPENID_NC_A"
    B = "OPENID_NC_B"

    @classmethod
    def setUpClass(cls):
        global bot
        import bot as _bot
        cls.bot = _bot
        cls.saved_owner = _bot.OWNER_OPENID

    def setUp(self):
        # 改名节流是按群记流水账的模块级状态，测试之间不清理会互相串味。
        self.bot.reset_nick_flood()
        self.bot.OWNER_OPENID = self.OWNER
        self.sent = []
        self._orig_reply = self.bot.safe_reply
        self._orig_judge = self.bot.judge_nick

        async def fake_reply(message, text):
            self.sent.append(text)

        async def fake_judge(nick):
            return None          # None = 放行，不因审核不可用而堵死本条测试

        self.bot.safe_reply = fake_reply
        self.bot.judge_nick = fake_judge
        self.bot.RELATIONS.set_nick(self.GID, self.A, "阿强", source="claim")
        self.bot.RELATIONS.set_nick(self.GID, self.B, "阿远", source="claim")

    def tearDown(self):
        self.bot.safe_reply = self._orig_reply
        self.bot.judge_nick = self._orig_judge
        self.bot.OWNER_OPENID = self.saved_owner
        prefix = f"{self.GID}|"
        for k in [k for k in list(self.bot.RELATIONS.records) if k.startswith(prefix)]:
            self.bot.RELATIONS.records.pop(k, None)
        self.bot.RENAMES.groups.pop(self.GID, None)

    def _nick_of(self, oid):
        return (self.bot.RELATIONS.get(self.GID, oid, create=False) or {}).get("nick")

    async def test_member_claiming_a_taken_name_is_refused(self):
        await self.bot.handle_nick_command(
            object(), {"scope": "self", "nick": "阿强"}, self.GID, self.B, False, [])
        self.assertEqual(self._nick_of(self.B), "阿远", "重名被挡了，但档案被动过了")
        self.assertIn("已经有人叫这个", self.sent[-1])

    async def test_owner_cannot_assign_a_taken_name(self):
        """群主的改名权也不能造出两个同名的人。"""
        await self.bot.handle_nick_command(
            object(), {"scope": "other", "nick": "阿强"}, self.GID, self.OWNER, True, [self.B])
        self.assertEqual(self._nick_of(self.B), "阿远")
        self.assertIn("已经有人叫这个", self.sent[-1])

    async def test_member_can_still_overwrite_his_own_name(self):
        """「随时可以覆盖」：本人改自己的名字一路放行，不能被自己的旧名挡住。"""
        await self.bot.handle_nick_command(
            object(), {"scope": "self", "nick": "阿强"}, self.GID, self.A, False, [])
        self.assertEqual(self._nick_of(self.A), "阿强")

    async def test_owner_can_still_rename_to_a_free_name(self):
        await self.bot.handle_nick_command(
            object(), {"scope": "other", "nick": "小满"}, self.GID, self.OWNER, True, [self.B])
        self.assertEqual(self._nick_of(self.B), "小满")


if __name__ == "__main__":
    unittest.main()
