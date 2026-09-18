"""「回复里不许出现 openid 尾巴」的回归测试。

为什么单开一个文件盯着这件事
----------------------------
「人类读不懂」不是审美问题，是**信息泄漏**：openid 是平台内部的账号标识，
群里看见「78D0」既不知道是谁、也不知道该怎么办 —— 而机器人还会一本正经地
让人去找这个人。

更麻烦的是它的传播方式：编号一旦进了喂给模型的文本（群聊背景、关系档案、
@ 的兜底文案），模型就会把它当成「某人的名字」原样复读出来。所以改一处没用，
得把**所有喂给模型的文本**和**所有发出去的文本**一起盯住。

这一组同时用行为断言和源码断言：前者盯这一次的输出，后者盯「将来别再有人
把编号拼回去」。
"""
import inspect
import re
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402
import prompts  # noqa: E402
import qqtext  # noqa: E402
import relations  # noqa: E402

GROUP = "GROUP_NO_RAW_IDS"
OWNER = "OPENID_NOIDS_OWNER"
TARGET = "FFFFFFFFFFFF78D0AAAAAAAAAAAAAAAA"

# openid 尾巴长这样：4 位十六进制。群里没人读得懂，也不该看见。
HEX_TAIL = re.compile(r"[0-9A-Fa-f]{4}")


class RelationNoteTest(unittest.TestCase):
    """关系档案是喂给模型的 —— 它复读什么，群里就看见什么。"""

    def test_other_names_come_without_tails(self):
        bot.RELATIONS.set_nick(GROUP, TARGET, "蒋泽明", source="owner")
        try:
            names = bot._other_names(GROUP, exclude_openid=OWNER)
        finally:
            bot.RELATIONS.records.pop(f"{GROUP}|{TARGET}", None)
        self.assertEqual(names, ["蒋泽明"], "对照表里只该有称呼，不该夹带编号")

    def test_note_lists_names_without_id_fragments(self):
        now = 10 ** 10
        note = relations.build_relation_note(
            # last_seen 贴近 now：否则「上次是 NNNN 个月前」这类正常的数字也会被
            # 四位十六进制的扫法误伤 —— 那是测试自己的噪声，不是产品的问题
            {"interactions": 5, "score": 2, "last_seen": now - 60},
            OWNER, mode="normal", now=now,
            other_names=["蒋泽明", "罗老板"])
        self.assertIn("蒋泽明", note, "别人认领过的称呼得告诉模型，不然会张冠李戴")
        self.assertIsNone(HEX_TAIL.search(note), f"关系档案里混进了编号：{note}")

    def test_note_still_accepts_legacy_pairs(self):
        """旧调用点传 (名字, 后四位) 二元组时，也不能把编号印出来。"""
        note = relations.build_relation_note(
            {"interactions": 5, "score": 2, "last_seen": 0.0},
            OWNER, mode="normal", now=10 ** 10,
            other_names=[("蒋泽明", "78D0")])
        self.assertIn("蒋泽明", note)
        self.assertNotIn("78D0", note)


class RenameReplyTest(unittest.IsolatedAsyncioTestCase):
    """给被人起名之后的确认语 —— 这是最早被吐槽「让我找 78D0」的那句。"""

    def setUp(self):
        # 同上：改名节流是模块级流水账，测试之间要清空。
        bot.reset_nick_flood()
        self.sent = []
        self._orig_reply = bot.safe_reply
        self._orig_judge = bot.judge_nick

        async def fake_reply(message, text, **_kw):
            self.sent.append(text)

        async def fake_judge(_nick):
            return None          # 审核不可用就放行，反正这里不关心它

        bot.safe_reply = fake_reply
        bot.judge_nick = fake_judge

    def tearDown(self):
        bot.safe_reply = self._orig_reply
        bot.judge_nick = self._orig_judge
        bot.RELATIONS.records.pop(f"{GROUP}|{TARGET}", None)
        bot.STATE.data.get("groups", {}).pop(GROUP, None)

    async def test_confirmation_never_shows_the_id_tail(self):
        cmd = {"scope": "other", "nick": "蒋泽明"}
        await bot.handle_nick_command(None, cmd, GROUP, OWNER, True, [TARGET])
        self.assertTrue(self.sent, "改完名总得回一句")
        self.assertIn("蒋泽明", self.sent[-1])
        self.assertNotIn(TARGET[-4:], self.sent[-1],
                         f"确认语里混进了 openid 尾巴：{self.sent[-1]}")
        self.assertIsNone(HEX_TAIL.search(self.sent[-1]),
                         f"确认语里混进了编号：{self.sent[-1]}")


class PromptRuleTest(unittest.TestCase):
    """提示词里得有一条明令：模型不许自己把编号说出来。"""

    def test_output_rules_ban_machine_ids(self):
        self.assertIn("openid", prompts.PROMPT_OUTPUT_RULES)
        self.assertIn("复读", prompts.PROMPT_OUTPUT_RULES)

    def test_mention_fallback_is_a_constant(self):
        """兜底必须是常量，不能是从 openid 上切下来的一截。"""
        self.assertNotIn("{}", qqtext.MENTION_FALLBACK)
        self.assertIsNone(HEX_TAIL.search(qqtext.MENTION_FALLBACK))


class NoIdSlicingInDisplayPathsTest(unittest.TestCase):
    """源码级守卫：把「显示给人看」的那几处钉死。

    行为断言只能证明这一次的输出干净；这条盯的是**别再把编号拼回去**。
    只查显示用的写法（`PROMISES.resolve` 那种拿 ID 当内部关键字的不算 —— 它不出口）。
    """

    def test_group_display_paths_do_not_slice_openids(self):
        src = inspect.getsource(bot.GroupBot.handle_group_msg)
        self.assertNotIn('群友{oid[-4:]}', src, "群聊背景又把编号拼回去了")
        self.assertNotIn('h["sender"][-4:]', src, "翻旧账又把编号拼回去了")

    def test_nick_command_does_not_slice_openids(self):
        # 只盯「拼进回复」的那一种：日志里留 openid 是给运维看的，那是它该在的地方
        src = inspect.getsource(bot.handle_nick_command)
        self.assertNotIn("{target[-4:]}", src, "改名确认语又把编号拼回去了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
