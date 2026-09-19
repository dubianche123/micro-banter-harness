"""压缩出来的名字，能不能对得上「最新的那一版」。

问题（用户提问）
----------------
模型看到的永远是**最新**的称呼（注入前过 `refresh_names`），但长期记忆、账本、每日 MD
里固化的是**压缩当时**那个名字。这两边对得上吗？逐条查下来的结论：

  · 改名（≥2 字）→ 对得上。`_sync_rename` 当场重写会话 / 长期记忆 / 每日 MD，
    注入前还有 `refresh_names` 兜底，连环改名靠台账永远解到当前称呼。
  · 单字名 → 对不上，**故意的**：一个字的名字到处都能撞上，替换误伤面太大。
  · 撤销称呼 → 退成占位符；旧版占位符里带 openid 后四位，这是**漏出去**的，见下。
  · 压缩时**没有名字的人** → 旧版拿 `sender[-4:]` 顶上，摘要里就多出「6ABA」这种人名，
    而摘要每轮注入 prompt，模型当群里真有这个人，照着复读。本轮修掉的主问题。
  · 账本（承诺催债）→ 旧版直接把存下来的名字拼进 prompt。本轮修掉。

所以这个文件盯的不是「改名同步」（那是 `test_rename.py` / `test_renames.py` 的事），
而是**压缩侧的署名与账本**：任何一处都不许把机器编号当成一个人名喂给模型。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402
import digest as digest_mod  # noqa: E402
import prompts  # noqa: E402
import relations  # noqa: E402

GID = "TESTGID_DIGEST_NAMES"
UNKNOWN = "E5E3793C25CF161D9F3292FE6ABA8C84"   # 没有任何称呼的人
KNOWN = "409365F491D38A25ADA2E37FD4886ABA"


class TranscriptNamingTest(unittest.TestCase):
    """压缩稿的署名。它是所有长期记忆的源头，这里进一串编号，后面就全脏了。"""

    def setUp(self):
        self.entries = [
            {"sender": UNKNOWN, "text": "这题超纲了", "ts": 1.0},
            {"sender": KNOWN, "text": "我就问问", "ts": 2.0},
        ]

    def test_unknown_speaker_is_not_an_id_tail(self):
        out = digest_mod.render_transcript(self.entries)
        self.assertIn(f"[{digest_mod.UNAMED_WHO}]", out)
        self.assertNotIn("6ABA", out, "又把 openid 后四位当人名了")
        self.assertNotIn(UNKNOWN, out)

    def test_resolver_wins_when_it_has_a_name(self):
        rows = digest_mod.render_transcript(
            self.entries, resolver=lambda oid: "阿强" if oid == UNKNOWN else None)
        self.assertIn("[阿强] 这题超纲了", rows)
        self.assertIn(f"[{digest_mod.UNAMED_WHO}] 我就问问", rows)

    def test_blocks_render_without_ids_too(self):
        """分块（map 阶段）之后每块也要渲染一次，两条路都得干净。"""
        blocks = digest_mod.GroupDigest().build_blocks({"pending": self.entries})
        joined = "\n".join(digest_mod.render_transcript(b) for b in blocks)
        self.assertNotIn("6ABA", joined)

    def test_digest_prompt_explains_the_placeholder(self):
        """模型得知道「一位群友」是泛称，不然它会当成某个人的外号写进人名表。"""
        self.assertIn(digest_mod.UNAMED_WHO, digest_mod.SYSTEM_REDUCE)


class ResolverChainTest(unittest.TestCase):
    """压缩时一个人叫什么：认领称呼 > 群昵称 > 没有（由压缩侧退成泛称）。"""

    def setUp(self):
        self.rows = dict(bot.RELATIONS.records)
        self.disp = dict(bot.DISPLAY_NAMES.groups)
        bot.RELATIONS.records.clear()
        bot.DISPLAY_NAMES.groups.clear()

    def tearDown(self):
        bot.RELATIONS.records.clear()
        bot.RELATIONS.records.update(self.rows)
        bot.DISPLAY_NAMES.groups.clear()
        bot.DISPLAY_NAMES.groups.update(self.disp)

    def test_nothing_known_returns_none(self):
        self.assertIsNone(bot._resolve_name(GID)(UNKNOWN),
                          "resolver 不该自己编一个编号出来")

    def test_group_display_name_is_used_as_a_fallback(self):
        bot.DISPLAY_NAMES.learn(GID, UNKNOWN, "贵阳老莫")
        self.assertEqual(bot._resolve_name(GID)(UNKNOWN), "贵阳老莫")

    def test_claimed_name_beats_the_group_display_name(self):
        bot.DISPLAY_NAMES.learn(GID, UNKNOWN, "贵阳老莫")
        bot.RELATIONS.set_nick(GID, UNKNOWN, "阿强", source="claim")
        self.assertEqual(bot._resolve_name(GID)(UNKNOWN), "阿强")


class ClearLabelTest(unittest.TestCase):
    """撤销称呼：历史文本里那个旧字面退成什么。旧版把编号写了进去，模型照抄过。"""

    def setUp(self):
        self.sid = f"{GID}_u1"
        bot.SESSIONS.record(self.sid, "机器人叫我阿龙", "记住了，以后叫你【阿龙】")
        bot.RELATIONS.set_nick(GID, UNKNOWN, "阿龙", source="claim")

    def tearDown(self):
        bot.SESSIONS._sessions.pop(self.sid, None)
        bot.RELATIONS.records.pop(f"{GID}|{UNKNOWN}", None)
        bot.RENAMES.groups.pop(GID, None)

    def _session_text(self):
        return "\n".join(m["content"] for m in bot.SESSIONS._sessions[self.sid]["messages"])

    def test_placeholder_constant_carries_no_id(self):
        self.assertEqual(relations.UNNAMED_LABEL, "（未留名）")

    def test_sync_clear_rewrites_history_without_an_id(self):
        # 真实调用顺序：先把称呼清空，再同步历史文本（否则会撞上「还是别人的主名」那条守卫）
        bot.RELATIONS.set_nick(GID, UNKNOWN, None, source="claim")
        self.assertTrue(bot._sync_clear(GID, UNKNOWN, "阿龙"))
        text = self._session_text()
        self.assertNotIn("阿龙", text, "撤销了称呼，历史里还在叫旧名")
        self.assertIn("未留名", text)
        self.assertNotIn("6ABA", text, "撤销后的占位符里混进了 openid 尾巴")


class PromisePayloadTest(unittest.IsolatedAsyncioTestCase):
    """催债：账本里存的是当时的称呼，拼进 prompt 之前必须刷新一遍。"""

    OLD = "阿龙"

    def setUp(self):
        bot.PROMISES.items[GID] = [{
            "id": "p1", "who": self.OLD, "what": "请大家喝奶茶",
            "since": 0.0, "due_ts": None, "due_text": "周五", "nag": 0, "last_nag": 0.0,
        }]
        self.captured = {}
        self._call = bot.call_model
        self._post = bot.post_to_group

        async def fake_call_model(messages, *a, **k):
            self.captured["payload"] = messages[-1]["content"]
            return "（催债测试）"

        async def fake_post(group_id, text):
            return True

        bot.call_model = fake_call_model
        bot.post_to_group = fake_post
        bot.RELATIONS.set_nick(GID, UNKNOWN, "阿强", source="claim")
        bot.RENAMES.note(GID, self.OLD, UNKNOWN)

    def tearDown(self):
        bot.call_model = self._call
        bot.post_to_group = self._post
        bot.PROMISES.items.pop(GID, None)
        bot.RELATIONS.records.pop(f"{GID}|{UNKNOWN}", None)
        bot.RENAMES.groups.pop(GID, None)

    async def test_nag_prompt_uses_the_current_name(self):
        await bot.dun_promises(GID)
        payload = self.captured.get("payload", "")
        self.assertTrue(payload, "没走到模型调用那一步，这条测试白测了")
        self.assertIn("阿强", payload, "催债时还在用旧名")
        self.assertNotIn(self.OLD, payload)


class PromptAntiTicTest(unittest.TestCase):
    """「下水道」「外卖」念叨个不停 —— 源头是提示词里的**固定例句**。

    这个项目的经验是「提示词里堆例句，模型会当模板抄」（文件头就写着）。可钓鱼应对那段
    还是留了两句现成话术和四个固定道具，于是「通下水道」「外卖最后到」成了口头禅 ——
    09-18 一天里 5 次下水道、8 次外卖，而且高度集中在这几个模板句上。
    这里钉住「别再把例句写回去」。
    """

    FIXED_PROPS = ("下水道", "外卖", "潜水服", "二哈", "拔火罐")

    def test_no_canned_props_in_any_prompt(self):
        text = "\n".join(v for k, v in vars(prompts).items()
                         if k.startswith("PROMPT_") and isinstance(v, str))
        for w in self.FIXED_PROPS:
            self.assertNotIn(w, text, f"提示词里又写死了「{w}」—— 它会被反复搬出来")

    def test_the_anti_tic_rule_is_in_the_shared_block(self):
        rule = prompts.PROMPT_SHARED_RULES
        self.assertIn("口头禅", rule)
        self.assertIn("第二次", rule)

    def test_the_shared_block_reaches_every_mode(self):
        """挂在共享规则里而不是某个模式里，换人设也甩不掉它。"""
        import inspect
        src = inspect.getsource(bot)
        self.assertIn("PROMPT_SHARED_RULES", src, "共享规则没被拼进稳定头")


class PromptCallbackAndAttributionTest(unittest.TestCase):
    """Gemini 上线一晚（2026-09-19 22:04-22:43，46 条）暴露的两类毛病：

    1. 旧账复读：46 条里「家豪封号」「近代史」「原理拿小本本」被翻出来 20+ 次，
       「少来这套，我可不接你这茬。当心原理在旁边拿着小本本…」几乎逐字出现 5 次 ——
       它把整个上下文窗口当成素材库，每条回复都回去捞一把。
    2. 张冠李戴：「嚷嚷着要当猫娘的人」一晚上被安给了至少三个不同的人，
       因为背景材料里只标了发言人、没说清「这些主语都不是眼前这位」。
    这里钉住两条新规则（都进共享块，换模型换人设都在）。
    """

    def test_callback_rule_is_in_the_shared_block(self):
        rule = prompts.PROMPT_SHARED_RULES
        self.assertIn("旧账", rule)
        self.assertIn("最多提一次", rule)
        self.assertIn("眼前这句话", rule)

    def test_attribution_rule_is_in_the_shared_block(self):
        rule = prompts.PROMPT_SHARED_RULES
        self.assertIn("拿不准是谁干的", rule)
        self.assertIn("别点名", rule)

    def test_context_hint_header_pins_the_speaker_boundary(self):
        """背景材料的引导语必须说清「主语不是眼前这位」—— 只靠共享规则不够，
        因为模型最容易犯浑的位置就是读背景那一刻。"""
        import inspect
        src = inspect.getsource(bot)
        self.assertIn("不是眼前这位说的", src)
        self.assertIn("拿不准是谁就别点名", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
