# -*- coding: utf-8 -*-
"""模型自主沉默权（2026-10-22 群主：Claude 给模型不说话的选项，小王也该有）。

此前「代码闸门决定调模型，过了闸模型必回」——被 @ 命中、插嘴骰子命中后，再不想
说话也得憋一句。现在 PROMPT_NORMAL 给了它 SKIP 出口（proactive 路径早有同款），
代码层只负责识别、留痕、不发送：
  · get_ai_reply 识别 SKIP → 返回 MODEL_SKIP 哨兵（先于乱码闸，哨兵永不被误炸）；
  · reply_in_session 收到哨兵 → 不发送、不落会话记录、返回 result=None；
  · 主流程 result=None → 跳过好感度结算（没搭话就没印象账可记）。
日志必须有 🤐 行 —— 这是「它自己装死」与「丢响应故障」在排查流程里的分界线。
"""
import asyncio
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "..")

import bot
import prompts


class GarbageGateNeverEatsSkipTest(unittest.TestCase):

    def test_skip_survives_garbage_detector(self):
        """哨兵是纯大写长串，乱码闸将来收紧也不能把沉默出口误炸掉。"""
        self.assertIsNone(bot._garbage_run(bot.MODEL_SKIP))

    def test_prompt_grants_the_right(self):
        self.assertIn("SKIP", prompts.PROMPT_NORMAL, "沉默出口要写进人设")
        self.assertIn("被 @", prompts.PROMPT_NORMAL,
                      "被点名时劝它尽量接 —— 装死一次拟人，被点名装死像坏了")

    def test_prompt_answers_knowledge_questions(self):
        self.assertIn("正经问题正经答", prompts.PROMPT_NORMAL)
        self.assertIn("别现编", prompts.PROMPT_NORMAL,
                      "不懂直说，回避与编造都不许")


class GetAiReplySkipTest(unittest.TestCase):
    """SKIP 走 get_ai_reply：原样透传为哨兵，且不被乱码重生成拉扯。"""

    def setUp(self):
        self._saved = bot.call_model
        self.calls = []

    def tearDown(self):
        bot.call_model = self._saved

    def test_skip_returns_sentinel_without_regeneration(self):
        async def fake_call(messages, max_tokens):
            self.calls.append(messages)
            return "SKIP"
        bot.call_model = fake_call

        body, delta = asyncio.run(
            bot.get_ai_reply("TESTSESSION_SKIP", "随便聊一句",
                             context_hint="", relation_note=""))
        self.assertEqual(body, bot.MODEL_SKIP)
        self.assertIsNone(delta)
        self.assertEqual(len(self.calls), 1, "沉默哨兵不该触发乱码重生成")

    def test_skip_is_case_insensitive(self):
        async def fake_call(messages, max_tokens):
            return "skip"
        bot.call_model = fake_call

        body, _ = asyncio.run(
            bot.get_ai_reply("TESTSESSION_SKIP2", "再聊一句",
                             context_hint="", relation_note=""))
        self.assertEqual(body, bot.MODEL_SKIP)


class ReplyInSessionSilentTest(unittest.TestCase):
    """reply_in_session 收到哨兵：不发送、返回 result=None。"""

    def setUp(self):
        self._saved_reply = bot.reply_in_session.__globals__["get_ai_reply"]
        self.sent = []

    def tearDown(self):
        bot.reply_in_session.__globals__["get_ai_reply"] = self._saved_reply

    def test_silent_reply_never_sends(self):
        async def fake_get_ai(session_id, user_text, allow_fallback=True, **ctx):
            return bot.MODEL_SKIP, None
        bot.reply_in_session.__globals__["get_ai_reply"] = fake_get_ai

        async def fake_safe_reply(message, reply_text, **kw):
            self.sent.append(reply_text)
            return object()

        message = SimpleNamespaceStub()
        with patch.object(bot, "safe_reply", fake_safe_reply):
            body, delta, result = asyncio.run(
                bot.reply_in_session(message, "TESTSESSION_SILENT", "在吗",
                                     context_hint="", relation_note=""))
        self.assertEqual(body, bot.MODEL_SKIP)
        self.assertIsNone(result, "沉默轮次没有送达结果")
        self.assertEqual(self.sent, [], "SKIP 绝不能变成一条发出的消息")


class SimpleNamespaceStub:
    group_openid = "TEST-SILENT-GROUP"


if __name__ == "__main__":
    unittest.main(verbosity=2)
