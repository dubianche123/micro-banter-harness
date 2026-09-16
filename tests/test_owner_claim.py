"""私聊认主的回归测试。

为什么单独一个文件盯着这件事
----------------------------
认领动作是这套机器人**最容易漏、而且漏了不报错**的一处：没人认领时一切照常，
只是所有群主特权静默失效，看起来像功能坏了。

而且「认领通道挪到私聊」本身就是一次安全修复。以前群里喊一句「我是群主」就永久生效 ——
等于把身份挂在大喇叭上招领，而且谁先喊谁得：随手一个群成员、甚至根本不在群里的人，
都能抢走它。下面每一条用例都对应这条边界上的一次踩坑。

⚠️ 认领会写 `owner.txt`。所有用例都必须把它指向临时文件 —— 碰了真的，
等于把线上群主换掉。
"""
import inspect
import os
import re
import sys
import tempfile
import types
import unittest

sys.path.insert(0, "..")

import config  # noqa: E402
import wordfilter  # noqa: E402

import bot  # noqa: E402

GROUP = "GROUP_OWNER_CLAIM"
OWNER = "OPENID_CLAIM_OWNER"
OTHER = "OPENID_CLAIM_OTHER"
BOT_ID = "BOT_ID_FOR_TEST"


class FakeUser:
    """群聊取 `member_openid`，私聊取 `user_openid` —— QQ 平台两套字段，同一个作者对象。"""

    def __init__(self, openid=None):
        self.member_openid = openid
        self.user_openid = openid


class FakeMessage:
    """够用的假消息体 —— 群聊和私聊各取自己需要的那几个字段。"""

    def __init__(self, content="", msg_id="MSG_1", group_id=GROUP,
                 member_openid=OWNER, mentions=()):
        self.id = msg_id
        self.content = content
        self.group_openid = group_id
        self.author = FakeUser(member_openid)
        self.mentions = list(mentions)

class FakeClient:
    """`handle_group_msg` 只用到 `self.robot` 这一个属性，其余不用造。"""

    def __init__(self):
        self.robot = types.SimpleNamespace(id=BOT_ID, name="")


class _StubSink:
    """把会写盘的归档/摘要换成空壳：测试不许碰真实群聊记录。"""

    def __init__(self):
        self.lines = []

    def append(self, *a, **kw):
        self.lines.append(a)

    def touch(self, *a, **kw):
        pass


class BaseClaimTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_owner = bot.OWNER_OPENID
        cls.saved_owner_file = config.OWNER_FILE
        cls.saved_phrase = config.OWNER_CLAIM_PHRASE
        cls.saved_archive = bot.ARCHIVE
        cls.saved_digests = bot.DIGESTS

    def setUp(self):
        # 认领会写 owner.txt —— 指向临时文件，绝不碰线上那一份
        self.tmpdir = tempfile.TemporaryDirectory()
        config.OWNER_FILE = os.path.join(self.tmpdir.name, "owner.txt")

        self.sent = []
        self._orig_reply = bot.safe_reply

        async def fake_reply(message, text):
            self.sent.append(text)

        bot.safe_reply = fake_reply
        bot.ARCHIVE = _StubSink()
        bot.DIGESTS = _StubSink()
        bot.OWNER_OPENID = None
        config.OWNER_CLAIM_PHRASE = self.saved_phrase
        # 清掉每群标记，让「只提示一次」这类用例彼此独立
        bot.STATE.data.get("groups", {}).pop(GROUP, None)
        self._mid = 0

    def tearDown(self):
        bot.safe_reply = self._orig_reply
        bot.OWNER_OPENID = self.saved_owner
        config.OWNER_FILE = self.saved_owner_file
        config.OWNER_CLAIM_PHRASE = self.saved_phrase
        bot.ARCHIVE = self.saved_archive
        bot.DIGESTS = self.saved_digests
        bot.RELATIONS.pinned.discard(OWNER)
        bot.STATE.data.get("groups", {}).pop(GROUP, None)
        self.tmpdir.cleanup()

    # ── 两个入口的便捷调用 ──

    def _next_id(self):
        """消息 id 必须每次都不一样：机器人按 id 去重，重复投递会被直接丢掉。

        上一版这里图省事写死了 id，结果全是「被去重」，测出来的失败是假的。
        """
        self._mid += 1
        return f"{self._testMethodName}-{self._mid}"

    async def say_in_group(self, text, sender=OTHER):
        await bot.GroupBot.handle_group_msg(
            FakeClient(), FakeMessage(text, msg_id=self._next_id(), member_openid=sender))

    async def say_in_private(self, text, sender=OWNER):
        await bot.GroupBot.on_c2c_message_create(
            FakeClient(), FakeMessage(text, msg_id=self._next_id(), member_openid=sender))


class GroupChatNeverClaimsTest(BaseClaimTest):
    """群里说破天也不授权 —— 这是这次修复的核心。"""

    async def test_claim_attempt_in_group_does_not_grant(self):
        await self.say_in_group("我是群主")
        self.assertIsNone(bot.OWNER_OPENID, "群里认领居然生效了 —— 大喇叭又回来了")
        self.assertEqual(os.path.exists(config.OWNER_FILE), False,
                         "群里认领居然还写了 owner.txt")

    async def test_group_path_contains_no_write_to_owner(self):
        """群聊路径里**不存在**任何写入 OWNER_OPENID 的语句。

        比一次行为断言更结实：将来谁顺手往群里加一句「那就认你吧」，这条会当场挂掉。
        用正则而不是子串 —— `OWNER_OPENID ==` 里也含「OWNER_OPENID =」，子串判断会误报。
        """
        src = inspect.getsource(bot.GroupBot.handle_group_msg)
        self.assertIsNone(re.search(r"OWNER_OPENID\s*=(?!=)", src),
                          "handle_group_msg 里出现了给 OWNER_OPENID 赋值的语句")

    async def test_claim_attempt_gets_redirected_to_private_chat(self):
        """不授权，但也不能当没听见 —— 当事人得找得到路。"""
        await self.say_in_group("我是群主")
        self.assertTrue(self.sent, "有人在群里试着认领，却连一句引导都没有")
        self.assertIn("私聊", self.sent[-1])

    async def test_redirect_only_happens_once_per_group(self):
        """反复喊不能反复刷屏。"""
        for _ in range(3):
            await self.say_in_group("我是群主")
        self.assertEqual(len(self.sent), 1, f"引导应该只回一次，实际回了 {len(self.sent)} 次")

    async def test_hint_never_contains_the_phrase(self):
        """引导语只负责指路。把暗号写进去，等于又请回一个大喇叭。"""
        self.assertNotIn(config.OWNER_CLAIM_PHRASE, bot.OWNER_CLAIM_HINT)

    async def test_group_probe_words_do_not_include_the_secret(self):
        """群里别拿真暗号当探测词：那会让群聊变成一台口令确认器。"""
        self.assertFalse(
            any(config.OWNER_CLAIM_PHRASE in w for w in bot.OWNER_CLAIM_PROBE_WORDS),
            f"探测词 {bot.OWNER_CLAIM_PROBE_WORDS} 里混进了真正的暗号")


class PrivateChatClaimTest(BaseClaimTest):
    """私聊才是认领通道，而且必须说对暗号。"""

    async def test_correct_phrase_claims_and_persists(self):
        await self.say_in_private(config.OWNER_CLAIM_PHRASE)
        self.assertEqual(bot.OWNER_OPENID, OWNER)
        with open(config.OWNER_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), OWNER, "认领结果没落到 owner.txt")
        self.assertIn(OWNER, bot.RELATIONS.pinned, "认领之后好感度该钉在顶格")

    async def test_phrase_is_inert_once_someone_else_claimed(self):
        """认领是一次性的：口令将来就算泄了，也换不掉主人。"""
        await self.say_in_private(config.OWNER_CLAIM_PHRASE)
        await self.say_in_private(config.OWNER_CLAIM_PHRASE, sender=OTHER)
        self.assertEqual(bot.OWNER_OPENID, OWNER, "第二位说对暗号的人把主人顶掉了")

    async def test_claim_is_not_transferable_and_says_nothing_about_who_owns_it(self):
        """被顶替的尝试：只回「有人认领过」，不透露是谁，也不说口令对不对。"""
        await self.say_in_private(config.OWNER_CLAIM_PHRASE)
        self.sent.clear()
        await self.say_in_private(config.OWNER_CLAIM_PHRASE, sender=OTHER)
        self.assertTrue(self.sent)
        self.assertNotIn(OWNER, self.sent[-1])

    async def test_owner_reclaiming_gets_a_short_answer(self):
        await self.say_in_private(config.OWNER_CLAIM_PHRASE)
        self.sent.clear()
        await self.say_in_private(config.OWNER_CLAIM_PHRASE)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(bot.OWNER_OPENID, OWNER)

    async def test_old_phrase_is_not_a_valid_phrase(self):
        """「我是群主」不再是口令 —— 它现在只会收到一句「得说对暗号」。"""
        self.assertNotEqual(config.OWNER_CLAIM_PHRASE, "我是群主")
        await self.say_in_private("我是群主")
        self.assertIsNone(bot.OWNER_OPENID)
        self.assertTrue(self.sent)
        self.assertIn("暗号", self.sent[-1])

    async def test_empty_phrase_disables_the_channel(self):
        """暗号留空 = 关掉私聊认主通道（那台机器只能手动写 owner.txt）。"""
        config.OWNER_CLAIM_PHRASE = ""
        await self.say_in_private("我是群主")
        self.assertIsNone(bot.OWNER_OPENID)
        self.assertTrue(self.sent)
        self.assertIn("通道", self.sent[-1])

    async def test_custom_phrase_wins(self):
        """换成自定义暗号之后，默认那句就该失效。"""
        config.OWNER_CLAIM_PHRASE = "芝麻开门"
        await self.say_in_private("我是群主")
        self.assertIsNone(bot.OWNER_OPENID)
        await self.say_in_private("芝麻开门")
        self.assertEqual(bot.OWNER_OPENID, OWNER)


class ClaimPhraseIsScrubbedTest(unittest.TestCase):
    """暗号本身当成敏感词注册掉。

    万一真有人在群里把它念出来，它就可能顺着摘要回流进长期记忆，之后每一轮 prompt 都
    带着它 —— 等于把口令抄进了模型上下文。这条钉住「注册」这个动作本身。
    """

    def test_phrase_is_registered_as_a_sensitive_word(self):
        self.assertTrue(wordfilter.has_hit(f"口令是{config.OWNER_CLAIM_PHRASE}"))
        self.assertEqual(wordfilter.scrub(f"口令是{config.OWNER_CLAIM_PHRASE}"),
                         "口令是【已隐去】")

    def test_short_phrase_is_rejected(self):
        """一个字的口令本来就该改掉，而且单字进词表误伤面太大。"""
        self.assertEqual(wordfilter.add_runtime_words(["字"]), 0)
        self.assertFalse(wordfilter.has_hit("这个字"))

    def test_normal_text_is_not_affected(self):
        self.assertFalse(wordfilter.has_hit("今天天气不错"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
