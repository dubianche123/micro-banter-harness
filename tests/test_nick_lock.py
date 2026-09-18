"""起名锁：群主一句话收走全群的自由起名。

由来（2026-09-18）
--------------------
改名节流拦的是「试得太快」，拦不住「慢慢试」—— 一天试两个，照样能磨过去。
锁是最后那道**手动**闸：群主看不下去的时候一句话把自由起名整个关掉，落名权收归自己
（他本来就能 @谁 说「叫 XXX」）。状态按群存，重启不丢。

三条铁律这里同样适用：
  · 没办成必须**明说**（被锁的人要听得懂为什么、该找谁）；
  · 群主的权力不受锁影响（他本来就有全群落名权）；
  · 撤销称呼不受锁影响 —— 后悔总得让人能后悔。
"""
import copy
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402
import relations  # noqa: E402

GID = "TESTGID_NICK_LOCK"
OWNER = "OPENID_LOCK_OWNER"
A = "OPENID_LOCK_A"


class ParserTest(unittest.TestCase):
    def test_lock_words(self):
        for text in ("小王锁起名", "锁起名", "把起名锁了", "禁止改名", "不准起名"):
            self.assertEqual(relations.parse_nick_lock_command(text), "lock", text)

    def test_unlock_words(self):
        for text in ("解锁起名", "开放起名", "恢复改名"):
            self.assertEqual(relations.parse_nick_lock_command(text), "unlock", text)

    def test_unlock_wins_over_the_lock_word_inside_it(self):
        """「解锁起名」里含着「锁起名」—— 解锁词必须先判，否则解不开。"""
        self.assertEqual(relations.parse_nick_lock_command("解锁起名"), "unlock")

    def test_ordinary_chat_is_not_a_toggle(self):
        for text in ("今天吃什么", "帮我起个名字", "别起名字了快跑", ""):
            self.assertIsNone(relations.parse_nick_lock_command(text), text)


class LockWiringTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        self.judged = []
        self._reply = bot.safe_reply
        self._judge = bot.judge_nick
        self._owner = bot.OWNER_OPENID

        async def fake_reply(message, text, **_kw):
            self.sent.append(text)

        async def fake_judge(nick):
            self.judged.append(nick)
            return None

        bot.safe_reply = fake_reply
        bot.judge_nick = fake_judge
        bot.OWNER_OPENID = OWNER
        bot.STATE.data.setdefault("groups", {}).pop(GID, None)

    def tearDown(self):
        bot.safe_reply = self._reply
        bot.judge_nick = self._judge
        bot.OWNER_OPENID = self._owner
        bot.STATE.data.setdefault("groups", {}).pop(GID, None)
        bot.RELATIONS.records.pop(f"{GID}|{A}", None)
        bot.RENAMES.groups.pop(GID, None)
        bot.reset_nick_flood()

    def _last(self):
        return self.sent[-1] if self.sent else ""

    async def _rename(self, sender=A, nick="阿强", is_owner=False, mentioned=()):
        await bot.handle_nick_command(
            None, {"scope": "other" if mentioned else "self", "nick": nick},
            GID, sender, is_owner, list(mentioned))

    async def test_only_the_owner_can_flip_the_switch(self):
        await bot.handle_nick_lock(None, GID, "lock", is_owner=False)
        self.assertFalse(bot.nick_locked(GID), "被普通人锁上了")
        self.assertIn("说了算", self._last())

    async def test_owner_locks_and_the_flag_persists(self):
        await bot.handle_nick_lock(None, GID, "lock", is_owner=True)
        self.assertTrue(bot.nick_locked(GID))
        self.assertTrue(
            bot.STATE.data["groups"][GID]["nick_locked"],
            "锁必须落在每群自己的槽位里，重启才不丢")
        self.assertIn("锁", self._last())

        # 再锁一次：别假装又办成了一回
        await bot.handle_nick_lock(None, GID, "lock", is_owner=True)
        self.assertIn("本来就", self._last())

    async def test_locked_group_refuses_member_self_rename(self):
        bot.set_nick_locked(GID, True)
        before = len(self.sent)
        await self._rename()
        self.assertEqual(len(self.judged), 0, "锁着呢还去调审核 —— 白花钱")
        self.assertEqual(
            (bot.RELATIONS.get(GID, A, create=False) or {}).get("nick"), None,
            "被锁挡住了，档案却动了")
        self.assertTrue(self.sent, "挡住了却不回话，用户只会以为机器人死机了")
        self.assertIn("锁", self._last())
        self.assertIn("点头", self._last(), "得告诉他该找谁")
        del before

    async def test_owner_can_still_rename_while_locked(self):
        bot.set_nick_locked(GID, True)
        await self._rename(sender=OWNER, nick="老王", is_owner=True)
        self.assertEqual((bot.RELATIONS.get(GID, OWNER, create=False) or {}).get("nick"), "老王")
        await self._rename(sender=OWNER, nick="小满", is_owner=True, mentioned=[A])
        self.assertEqual((bot.RELATIONS.get(GID, A, create=False) or {}).get("nick"), "小满",
                         "群主给人落名的权不该被锁捆住")

    async def test_member_can_still_clear_his_name_while_locked(self):
        bot.set_nick_locked(GID, True)
        bot.RELATIONS.set_nick(GID, A, "阿强", source="claim")
        await bot.handle_nick_command(
            None, {"scope": "self-clear"}, GID, A, False, [])
        self.assertIsNone((bot.RELATIONS.get(GID, A, create=False) or {}).get("nick"),
                          "撤销称呼也被锁挡住 —— 后悔都不让人后悔")

    async def test_unlock_restores_renames(self):
        bot.set_nick_locked(GID, True)
        await bot.handle_nick_lock(None, GID, "unlock", is_owner=True)
        self.assertFalse(bot.nick_locked(GID))
        await self._rename()
        self.assertEqual((bot.RELATIONS.get(GID, A, create=False) or {}).get("nick"), "阿强")

    async def test_unlock_when_not_locked_is_told_out_loud(self):
        await bot.handle_nick_lock(None, GID, "unlock", is_owner=True)
        self.assertIn("本来就没锁", self._last())


class PrivateChatHintTest(unittest.TestCase):
    """锁是按群存在的，私聊没有群号 —— 和改名一样必须指回群里，不能装没听见。"""

    def test_private_path_checks_the_lock_command(self):
        import inspect
        src = "\n".join(
            line.split("#", 1)[0]
            for line in inspect.getsource(bot.GroupBot.on_c2c_message_create).splitlines()
        ) if hasattr(bot.GroupBot, "on_c2c_message_create") else ""
        if not src:
            self.skipTest("私聊入口不在这个类上")
        self.assertIn("parse_nick_lock_command", src,
                      "私聊路径没接起名开关的指路话术")


if __name__ == "__main__":
    unittest.main(verbosity=2)
