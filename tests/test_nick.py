"""昵称指令解析 + 艾特对象提取的回归测试。

每一条用例都对应一次真实踩坑，改规则前先跑一遍：python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relations import parse_nick_command  # noqa: E402

import naming  # noqa: E402
import relations  # noqa: E402


BOT = ("小星",)
OTHER = ["AAAAAAAAAAAAAAAAAAAAAAAAAAAA1111"]   # 被艾特的那位的 openid


class FakeUser:
    def __init__(self, member_openid=None, oid=None):
        self.member_openid = member_openid
        self.id = oid


class FakeMessage:
    def __init__(self, content="", mentions=()):
        self.content = content
        self.mentions = list(mentions)


class ParseNickTest(unittest.TestCase):
    def check(self, text, mentioned=(), expect_scope=None, expect_nick=None):
        got = parse_nick_command(text, bot_names=BOT, mentioned_others=mentioned)
        if expect_scope is None:
            self.assertIsNone(got, f"「{text}」不该被当成改名指令，实际得到 {got}")
            return
        self.assertIsNotNone(got, f"「{text}」应该被识别成 {expect_scope}，实际是 None")
        self.assertEqual(got["scope"], expect_scope, f"「{text}」范围判错：{got}")
        if expect_nick is not None:
            self.assertEqual(got["nick"], expect_nick, f"「{text}」名字判错：{got}")

    # ── 给自己起名（没艾特任何人）──
    def test_self_forms(self):
        self.check("小星，叫 小满", (), "self", "小满")
        self.check("叫我阿强", (), "self", "阿强")
        self.check("以后叫我阿强", (), "self", "阿强")
        self.check("我叫小满", (), "self", "小满")
        self.check("老张，叫我阿强", (), "self", "阿强")

    def test_self_not_triggered_by_noise(self):
        self.check("叫个外卖", ())
        self.check("叫我去群里看看", ())
        self.check("叫我也为难啊", ())
        self.check("我叫你别闹了", ())

    # ── 疑问句绝不能被当成改名（今天真实事故）──
    def test_questions_never_rename(self):
        """「小星我叫什么名字」曾把名字改成「什么名字」——比没记住严重得多。"""
        self.check("小星我叫什么名字", ())
        self.check("小星我叫啥", ())
        self.check("小星你叫什么名字", ())
        self.check("我叫谁", ())
        self.check("我叫什么好呢", ())

    # ── 自报家门「我是XX」──
    def test_self_intro(self):
        self.check("小星我是阿远，记住了", (), "self", "阿远")
        self.check("我是阿龙", (), "self", "阿龙")
        self.check("以后我是阿强", (), "self", "阿强")

    def test_self_intro_rejects_impostors(self):
        """冒名和别人名字不能被当称呼。"""
        self.check("小星我是你爹", ())
        self.check("我是群主", ())

    # ── 艾特了别人：名字归被艾特的那位 ──
    def test_at_other_with_verb(self):
        self.check("小星，叫@阿澈 小满", OTHER, "other", "小满")
        self.check("@阿澈 叫他阿强", OTHER, "other", "阿强")
        self.check("以后叫他阿强", OTHER, "other", "阿强")
        self.check("叫 小满", OTHER, "other", "小满")

    def test_at_other_bare_name(self):
        self.check("@阿澈 小满", OTHER, "other", "小满")
        self.check("@阿澈 阿强", OTHER, "other", "阿强")

    def test_at_other_noise_rejected(self):
        self.check("@阿澈 收到", OTHER)
        self.check("@阿澈 666", OTHER)
        self.check("@阿澈 在吗", OTHER)
        self.check("@阿澈 你昨天说的那个", OTHER)

    def test_at_other_but_self_intent(self):
        """艾特了别人，但明说「叫我」——那还是给自己起。"""
        self.check("@阿澈 叫我阿强", OTHER, "self", "阿强")

    def test_clear_own_nick(self):
        self.check("取消我的称呼", (), "self-clear")
        self.check("别叫我了", (), "self-clear")

    def test_non_owner_at_is_not_naming(self):
        """普通群友艾特别人说话，不该被当成起名指令（调用侧传空 mentioned）。"""
        self.check("小星，叫@阿澈 小满", ())


class AllowOtherTest(unittest.TestCase):
    """「谁能给别人起名」的边界。

    这条边界原先是靠「非群主就不把被 @ 的人传进解析」实现的，副作用是连
    「@阿澈 叫我阿强」这种本人改名也跟着失效 —— @ 文本没人剥，前缀对不上。
    现在改成 allow_other 参数：@ 了谁是事实，照传；能不能给别人起名单独控。
    """

    def parse(self, text, mentioned=(), allow_other=False):
        return parse_nick_command(text, bot_names=BOT,
                                  mentioned_others=mentioned, allow_other=allow_other)

    def test_owner_can_name_others(self):
        """群主 @某人 起名：带动词、光甩名字两种句式都认。"""
        for text in ("@阿澈 叫他阿强", "@阿澈 阿强", "@阿澈 叫 阿强"):
            got = self.parse(text, OTHER, allow_other=True)
            self.assertIsNotNone(got, f"群主「{text}」应当识别为给别人起名")
            self.assertEqual((got["scope"], got["nick"]), ("other", "阿强"), text)

    def test_member_cannot_name_others(self):
        """普通群友不能给别人（尤其群主）起名，@ 了也不行。"""
        for text in ("@群主 叫他狗蛋", "@群主 狗蛋", "@阿澈 阿强"):
            got = self.parse(text, OTHER, allow_other=False)
            self.assertIsNone(got, f"普通群友「{text}」不该被当成命名指令，实际得到 {got}")

    def test_member_can_still_rename_self_while_mentioning(self):
        """@ 了别人也照样能给自己改名 —— 这正是旧实现漏掉的那条路径。"""
        for text in ("@阿澈 叫我阿强", "@阿澈 我是阿强"):
            got = self.parse(text, OTHER, allow_other=False)
            self.assertIsNotNone(got, f"「{text}」应当识别为本人改名，实际是 None")
            self.assertEqual((got["scope"], got["nick"]), ("self", "阿强"), text)

    def test_without_mention_both_roles_agree(self):
        """没艾特任何人时，两种身份走同一条给自己改名的路。"""
        for allow in (True, False):
            got = self.parse("我是阿强", (), allow_other=allow)
            self.assertEqual((got["scope"], got["nick"]), ("self", "阿强"))


class ReservedNameTest(unittest.TestCase):
    """「这几个名字已经有主了」：机器人自己的名字 + 群主当前的称呼。

    刻意不写死任何人名 —— 名字全部按**当前实际叫什么**现取，所以下面的断言之于
    具体叫什么完全无关：机器人改个名、群主换个外号，这些测试照样成立。
    """

    GROUP = "GROUP_RESERVED"
    OWNER = "OPENID_OWNER"
    MEMBER = "OPENID_MEMBER"

    @classmethod
    def setUpClass(cls):
        import bot
        cls.bot = bot
        cls.saved_owner = bot.OWNER_OPENID

    def setUp(self):
        self.bot.OWNER_OPENID = self.OWNER
        self.bot.RELATIONS.get(self.GROUP, self.OWNER)["nick"] = "老张"

    def tearDown(self):
        self.bot.OWNER_OPENID = self.saved_owner
        self.bot.RELATIONS.records.pop(f"{self.GROUP}|{self.OWNER}", None)

    def _bad(self, nick, claimant):
        return relations.bad_nick(
            nick,
            reserved=self.bot._reserved_nick_names(self.GROUP, claimant),
            taken=self.bot._taken_nick_names(self.GROUP, claimant),
        )

    def test_member_cannot_take_owner_nick(self):
        """顶着群主的外号在群里招摇，是冒名顶替里最典型的一种。"""
        self.assertIsNotNone(self._bad("老张", self.MEMBER))

    def test_member_cannot_take_bot_name(self):
        """机器人的名字谁都不能占 —— 那个位置只有一个。"""
        self.assertIsNotNone(self._bad(naming.bot_names()[0], self.MEMBER))

    def test_owner_can_still_be_himself(self):
        """群主给自己改名不该被自己现在的称呼挡住（否则他改不回去）。"""
        self.assertIsNone(self._bad("老张", self.OWNER))

    def test_owner_cannot_take_bot_name_either(self):
        self.assertIsNotNone(self._bad(naming.bot_names()[0], self.OWNER))

    def test_owner_nick_stops_being_reserved_after_rename(self):
        """群主改了称呼，旧名字立刻就不再是「有主」的 —— 不留历史包袱。"""
        self.bot.RELATIONS.get(self.GROUP, self.OWNER)["nick"] = "老李"
        self.assertIsNone(self._bad("老张", self.MEMBER))
        self.assertIsNotNone(self._bad("老李", self.MEMBER))


class NameCollisionTest(unittest.TestCase):
    """主名不能重叠：别人占着的主名，第二个人不能再拿。

    但「随时可以覆盖」不能被这条挡住 —— 本人换掉自己的名字要一路放行，
    群主给被 @ 的那位改名同理（被改的那位自己那一版不算被别人占用）。
    占用是按 **openid** 排的，不是按字面排，所以这两种情况分得开。
    """

    GROUP = "GROUP_COLLISION"
    OWNER = "OPENID_OWNER_C"
    A = "OPENID_COLLIDE_A"
    B = "OPENID_COLLIDE_B"

    @classmethod
    def setUpClass(cls):
        import bot
        cls.bot = bot
        cls.saved_owner = bot.OWNER_OPENID

    def setUp(self):
        self.bot.OWNER_OPENID = self.OWNER
        R = self.bot.RELATIONS
        R.set_nick(self.GROUP, self.A, "阿强", source="claim")
        R.set_nick(self.GROUP, self.B, "阿远", source="claim")

    def tearDown(self):
        self.bot.OWNER_OPENID = self.saved_owner
        for oid in (self.A, self.B):
            self.bot.RELATIONS.records.pop(f"{self.GROUP}|{oid}", None)

    def _bad(self, nick, subject):
        b = self.bot
        return relations.bad_nick(
            nick,
            reserved=b._reserved_nick_names(self.GROUP, subject),
            taken=b._taken_nick_names(self.GROUP, subject),
        )

    def test_cannot_take_another_members_name(self):
        self.assertEqual(self._bad("阿远", self.A), "群里已经有人叫这个了，换一个")

    def test_owner_cannot_give_a_name_someone_else_already_holds(self):
        """群主权威再大也不能造出两个同名的人 —— 那样机器人就分不清谁是谁。"""
        self.assertEqual(self._bad("阿强", self.B), "群里已经有人叫这个了，换一个")

    def test_self_can_overwrite_own_name(self):
        """随时可以覆盖：本人重设自己的名字，不该被自己现在这一版挡住。"""
        self.assertIsNone(self._bad("阿强", self.A))

    def test_owner_can_still_rename_target_to_a_free_name(self):
        self.assertIsNone(self._bad("小满", self.B))

    def test_owner_may_reassign_the_targets_own_name(self):
        """群主重复写同一位自己的名字（无变化的那种）也不该被拦。"""
        self.assertIsNone(self._bad("阿远", self.B))

    def test_renaming_frees_the_old_name_for_others(self):
        """本人一改名，旧名字立刻空出来，别人就能用了 —— 不留历史包袱。"""
        self.bot.RELATIONS.set_nick(self.GROUP, self.A, "小满", source="claim")
        self.assertIsNone(self._bad("阿强", self.B))

    def test_collision_message_differs_from_impersonation(self):
        """两种拒绝理由要说不一样的话：群主撞上的是重名，不是冒名顶替。"""
        taken_reason = self._bad("阿远", self.A)
        reserved_reason = self._bad(naming.bot_names()[0], self.A)
        self.assertNotEqual(taken_reason, reserved_reason)


class ExtractMentionsTest(unittest.TestCase):
    def test_prefers_event_mentions(self):
        from bot import extract_mentions
        msg = FakeMessage("小星，叫@阿澈 小满",
                          [FakeUser(member_openid="OPENID_MIYU")])
        self.assertEqual(extract_mentions(msg, "BOTID"), ["OPENID_MIYU"])

    def test_falls_back_to_placeholder(self):
        from bot import extract_mentions
        msg = FakeMessage("喂 <@!ABC123> 说句话")
        self.assertEqual(extract_mentions(msg, "BOTID"), ["ABC123"])

    def test_excludes_bot_itself(self):
        from bot import extract_mentions
        msg = FakeMessage("hi", [FakeUser(member_openid="BOTID"),
                                 FakeUser(member_openid="HUMAN")])
        self.assertEqual(extract_mentions(msg, "BOTID"), ["HUMAN"])

    def test_plain_text_at_yields_nothing(self):
        """纯明文 @昵称 拿不到 ID，宁可空着也不要瞎猜。"""
        from bot import extract_mentions
        msg = FakeMessage("小星，叫@阿澈 小满")
        self.assertEqual(extract_mentions(msg, "BOTID"), [])


class LooksLikeOtherNickTest(unittest.TestCase):
    """「这句是不是在给别人改名」——必须是**强信号**，宁可漏也别误伤。

    为什么要单拎出来：`parse_nick_command` 会把普通群友的 other 意图按权限整个
    吞掉（`allow_other=False`），于是请求掉进闲聊 —— 机器人顺着接一句
    「别别别，这辈分乱套了」，群里看着像在商量、甚至像改成了，其实一个字没存。
    所以需要一次独立探测来把这种请求**明确回绝**。

    代价是误伤：把「@老王 你好」当成起名，每句打招呼都要被回绝一次，比不响还烦。
    所以只认带「叫/称呼/改名」这类动词的句式，不认光秃秃的短句。
    """

    MENTIONED = ["BBBBBBBBBBBBBBBBBBBBBBBBBBBB2222"]

    def test_verb_with_mention_is_an_intent(self):
        self.assertTrue(relations.looks_like_other_nick(
            "小星，叫@阿澈 儿子", bot_names=BOT, mentioned_others=self.MENTIONED))

    def test_verb_without_mention_is_also_an_intent(self):
        """「叫他阿强」没 @ 人，但同样是明确意图 —— 一样不该静默掉进闲聊。"""
        self.assertTrue(relations.looks_like_other_nick("叫他阿强", bot_names=BOT))

    def test_subject_before_the_verb_is_an_intent(self):
        """真实语料：「小王他叫王洪文，记住了@老王」。

        陈述句的形式、祈使的意图（「你记住，他叫这个」）。少了「他」前面这一位，
        整句识别不出来，请求掉进闲聊 —— 机器人复读一遍名字说「知道了」，其实没改名。
        """
        self.assertTrue(relations.looks_like_other_nick(
            "小星他叫王洪文，记住了@老王", bot_names=BOT, mentioned_others=self.MENTIONED))

    def test_self_intent_is_not_read_as_renaming_someone_else(self):
        """「我叫小满」是自报家门，别回一句「你没权限给别人改名」。"""
        self.assertFalse(relations.looks_like_other_nick(
            "我叫小满", bot_names=BOT, mentioned_others=self.MENTIONED))

    def test_question_is_not_an_intent(self):
        """「他叫什么」是提问 —— 疑问词表在 _clean_nick 里。"""
        self.assertFalse(relations.looks_like_other_nick(
            "小星他叫什么", bot_names=BOT, mentioned_others=self.MENTIONED))

    def test_bare_phrase_after_at_is_not_an_intent(self):
        """「@老王 你好」不算 —— `RE_AT_BARE` 是「任意 1-12 字」，认了就全中。"""
        self.assertFalse(relations.looks_like_other_nick(
            "小星，@老王 你好", bot_names=BOT, mentioned_others=self.MENTIONED))

    def test_ordinary_chat_is_not_an_intent(self):
        self.assertFalse(relations.looks_like_other_nick(
            "小星，今天天气不错", bot_names=BOT, mentioned_others=self.MENTIONED))

    def test_calling_a_thing_is_not_a_rename(self):
        """「叫个外卖」这种，动词后面不是名字。"""
        self.assertFalse(relations.looks_like_other_nick(
            "叫个外卖", bot_names=BOT))


class RenameWordingTest(unittest.TestCase):
    """群主赐名的确认语：要挡住旁人，但**不能**把本人也挡在门外。

    用户原话：「『{owner}御赐的名字，谁也不许改』这句话一说，搞得好像他本人也
    改不了一样」。事实上本人一句「叫我 XXX」就能覆盖（source=claim 同样是合法
    写入源），所以措辞必须把「旁人动不了」和「本人能改」分开说。
    """

    def test_confirmation_does_not_lock_the_person_out(self):
        import inspect
        import re

        import bot

        # 注释里会拿这句当反例讲「为什么不能这么写」，那正是要留下的说明，
        # 所以只扫**真正会发出去**的部分：把 # 注释剥掉再断言。
        src = inspect.getsource(bot.handle_nick_command)
        code = "\n".join(re.sub(r"#.*$", "", line) for line in src.splitlines())
        for banned in ("谁也不许改", "谁也不许擦", "谁也改不了", "谁都改不了"):
            self.assertNotIn(banned, code, f"「{banned}」听起来像本人也改不动")
        self.assertIn("旁人", code, "挡的应该是旁人，不是本人")
        self.assertIn("本人想改", code, "得给本人留一句「随时能改」的出口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
