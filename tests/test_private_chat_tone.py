"""私聊的语气分寸，以及「对方喊停就立刻收」。

两个问题的由来（2026-09-18 真实对话）
------------------------------------
私聊里机器人原样套用了群聊那套毒舌 —— 群里的损是「有观众的玩笑」：有人起哄、有人接梗、
下一句就被顶回去。私聊是一对一，没有观众也没有承接，同一句砸下去就是单方面的攻击。

   我好累，加班加到两点半
   → 才两点半？这才哪到哪……别把自己那点肝熬没了
   别这么损我，项目快上线了
   → 这时候想起上线了？早干嘛去了。别在这儿浪费时间求安慰。      ← 加码
   我草你有点过分了
   → 这就过分了？你平时损我的时候也没见手软啊。                  ← 嘴硬

第二句和第三句是最不能接受的：**对方明说不舒服，它反而更狠**。所以喊停必须本地硬拦，
交给模型自觉来不及 —— 它已经在错误方向上跑了几轮。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402
import prompts  # noqa: E402


class BackOffDetectionTest(unittest.TestCase):
    """「他是不是在喊停」。判宽一点：误判只是这次不开玩笑，漏判是把人推远。"""

    def test_explicit_ask_to_stop_mocking(self):
        for text in ("别这么损我，项目快上线了", "别嘲我了", "别再怼我了", "不要这样说我"):
            with self.subTest(text=text):
                self.assertTrue(bot.looks_like_back_off(text), text)

    def test_saying_it_went_too_far(self):
        for text in ("我草你有点过分了", "你太过分了吧", "这话有点伤人", "说得有点重了"):
            with self.subTest(text=text):
                self.assertTrue(bot.looks_like_back_off(text), text)

    def test_asking_to_be_serious(self):
        for text in ("认真点，我在说正事", "正经点", "我不是开玩笑"):
            with self.subTest(text=text):
                self.assertTrue(bot.looks_like_back_off(text), text)

    def test_ordinary_chat_is_not_a_stop_signal(self):
        """误判的代价是机器人变正经，群里天天误判人设就没了。"""
        for text in ("今天天气不错", "小王 在吗", "帮我看个 bug", "这波操作有点秀"):
            with self.subTest(text=text):
                self.assertFalse(bot.looks_like_back_off(text), text)

    def test_plain_joking_is_not_a_stop_signal(self):
        """「别闹了」是群里最常见的玩笑话，收了它机器人就天天端着。"""
        self.assertFalse(bot.looks_like_back_off("别闹了，说正事"))

    def test_the_real_lines_from_the_incident(self):
        """当天那三句，至少第二、第三句必须被认出来。"""
        self.assertFalse(bot.looks_like_back_off("我好累，加班加到两点半"))
        self.assertTrue(bot.looks_like_back_off("别这么损我，项目快上线了"))
        self.assertTrue(bot.looks_like_back_off("我草你有点过分了"))


class PromptWiringTest(unittest.IsolatedAsyncioTestCase):
    """规则得真的进到本轮信封里 —— 光写在 prompts.py 里不生效。"""

    async def _capture_turn_context(self, **kw):
        """跑一次 get_ai_reply，把模型看到的最后一条 user 消息抓回来。"""
        seen = {}

        async def fake_call_model(messages, *a, **k):
            seen["last_user"] = "\n".join(
                m.get("content", "") for m in messages if m.get("role") == "user") or ""
            return "（测试替身）"

        orig = bot.call_model
        bot.call_model = fake_call_model
        try:
            await bot.get_ai_reply("SESSION_TEST_TONE", kw.pop("user_text", "在吗"), **kw)
        finally:
            bot.call_model = orig
        return seen.get("last_user", "")

    async def test_private_chat_gets_its_own_rules(self):
        text = await self._capture_turn_context(private=True, user_text="今天挺累的")
        self.assertIn("只有你和他两个人", text, "私聊没拿到自己的语气规则")

    async def test_group_chat_keeps_the_group_tone(self):
        """群里不该被私聊那套约束住 —— 那本来就是互动方式。"""
        text = await self._capture_turn_context(private=False, user_text="今天挺累的")
        self.assertNotIn("只有你和他两个人", text, "私聊规则漏进群里了")

    async def test_back_off_overrides_everything(self):
        text = await self._capture_turn_context(
            private=False, is_owner=True, user_text="别这么损我，项目快上线了")
        self.assertIn(prompts.BACK_OFF_MARK, text, "喊停了却没下收敛指令")
        # 而且必须压在「该损就损」那句后面
        self.assertLess(text.find(prompts.PROMPT_OWNER_NOTE.strip()[:12]),
                        text.find(prompts.BACK_OFF_MARK),
                        "收敛指令必须排在 owner note 之后才压得住")

    async def test_ordinary_line_gets_no_back_off(self):
        text = await self._capture_turn_context(private=False, user_text="今天天气不错")
        self.assertNotIn(prompts.BACK_OFF_MARK, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
