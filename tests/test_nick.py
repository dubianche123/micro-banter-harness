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


if __name__ == "__main__":
    unittest.main(verbosity=2)
