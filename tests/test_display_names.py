"""群昵称（群里平台侧的显示名）的回归测试。

为什么需要它
------------
事件体只给 openid，官方的群成员接口又要单独申请权限（实测 11253 无权限），
所以「群里怎么显示这个人」只能从正文里 @ 人时留下的**明文昵称**学。

这条路有个天然陷阱：**多人 @ 时无法判断哪个名字对应哪个 openid**。
挂错人比不知道更糟 —— 机器人会当众用错名字叫人。所以只在「一条消息只 @ 了
一个人」时才记，这一组用例主要就是钉这件事。

另外它是**显示兜底**：认领过的称呼永远优先，它不参与判断、不进模型上下文。
"""
import inspect
import re
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402
import relations  # noqa: E402

GROUP = "GROUP_DISPLAY_NAMES"
ALICE = "OPENID_DN_ALICE"
BOB = "OPENID_DN_BOB"


class ContentParsingTest(unittest.TestCase):
    """从正文里抠昵称 —— 切到空白为止，宁可短也不猜错边界。"""

    def test_plain_nick(self):
        self.assertEqual(bot.mention_nick_from_content("@奶龙 在吗"), "奶龙")

    def test_nick_with_spaces_is_trimmed_at_the_first_break(self):
        """「A.A 贵阳老莫（全国可飞）」这种只能拿到第一段 —— 边界猜不得。"""
        self.assertEqual(
            bot.mention_nick_from_content("小王，叫@A.A 贵阳老莫（全国可飞） 老王"), "A.A")

    def test_nick_inside_a_rename_command(self):
        self.assertEqual(bot.mention_nick_from_content("小王，叫@米浴 家豪"), "米浴")

    def test_trailing_dot_is_not_part_of_the_nick(self):
        """句末的句点不算名字 —— 但名字里的句点要留住（A.A / L.L）。"""
        self.assertEqual(bot.mention_nick_from_content("@奶龙. 在吗"), "奶龙")
        self.assertEqual(bot.mention_nick_from_content("@L.L 来一下"), "L.L")

    def test_bot_itself_is_not_a_nickname(self):
        """@机器人是在喊它，不是在提某个人。"""
        self.assertIsNone(bot.mention_nick_from_content("@小王 你好", bot_names=("小王",)))
        # 群里喊的是带后缀的叫法，也要认出来是它自己
        self.assertIsNone(
            bot.mention_nick_from_content("@小王的分身 在吗", bot_names=("小王",)))

    def test_fallback_placeholder_is_not_a_nickname(self):
        self.assertIsNone(bot.mention_nick_from_content("@群友 这是谁"))

    def test_no_mention_returns_none(self):
        self.assertIsNone(bot.mention_nick_from_content("今天天气不错"))


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.dn = relations.DisplayNames()

    def test_learns_and_reads_back(self):
        self.assertTrue(self.dn.learn(GROUP, ALICE, "奶龙"))
        self.assertEqual(self.dn.of(GROUP, ALICE), "奶龙")

    def test_single_char_is_refused(self):
        """单字误伤面太大，跟台账一个规矩。"""
        self.assertFalse(self.dn.learn(GROUP, ALICE, "龙"))
        self.assertIsNone(self.dn.of(GROUP, ALICE))

    def test_unchanged_name_does_not_dirty_the_state(self):
        self.assertTrue(self.dn.learn(GROUP, ALICE, "奶龙"))
        self.assertFalse(self.dn.learn(GROUP, ALICE, "奶龙"), "没变化的重记不该标脏")

    def test_names_are_per_group(self):
        self.dn.learn(GROUP, ALICE, "奶龙")
        self.assertIsNone(self.dn.of("别的群", ALICE))

    def test_survives_a_restart(self):
        self.dn.learn(GROUP, ALICE, "奶龙")
        data = {}
        self.dn.dump_into(data)
        fresh = relations.DisplayNames()
        fresh.hydrate(data.get("display_names"))
        self.assertEqual(fresh.of(GROUP, ALICE), "奶龙")


class PairingTest(unittest.TestCase):
    """明文 @ 与被 @ 的 openid 怎么配成一对。

    这是整条功能最容易出事的地方：**挂错人比不知道更糟** —— 机器人会当众用
    别人的名字叫你。所以规则是「数量对得上才按顺序配」，对不上就整条放弃。
    """

    def setUp(self):
        self.saved = bot.DISPLAY_NAMES
        bot.DISPLAY_NAMES = relations.DisplayNames()

    def tearDown(self):
        bot.DISPLAY_NAMES = self.saved

    def test_two_mentions_pair_in_order(self):
        """日常互相 @ 是大头，不能只认单 @。"""
        got = bot.learn_display_names(
            GROUP, [ALICE, BOB], "@奶龙 @老莫 你们俩来一下")
        self.assertEqual(got, 2)
        self.assertEqual(bot.DISPLAY_NAMES.of(GROUP, ALICE), "奶龙")
        self.assertEqual(bot.DISPLAY_NAMES.of(GROUP, BOB), "老莫")

    def test_at_the_bot_does_not_shift_the_pairing(self):
        """@机器人会同时出现在 mentions 和正文里 —— 两边都要剔掉，否则整体错位。

        归档实证：`小王，叫@米浴 家豪`，`at:1`（@ 了机器人 + @ 了米浴）。
        """
        bot.learn_display_names(GROUP, [BOB], "小王，叫@米浴 家豪", bot_names=("小王",))
        self.assertIsNone(bot.DISPLAY_NAMES.of(GROUP, ALICE))
        self.assertEqual(bot.DISPLAY_NAMES.of(GROUP, BOB), "米浴")

    def test_hand_typed_fake_mention_is_not_guessed(self):
        """手打了个「@老王」但没真 @ —— 明文比 openid 多，宁可不记。"""
        got = bot.learn_display_names(GROUP, [ALICE], "@奶龙 顺便@路人甲 也来")
        self.assertEqual(got, 0, "数量对不上就别猜，猜错会当众叫错人")
        self.assertIsNone(bot.DISPLAY_NAMES.of(GROUP, ALICE))

    def test_placeholder_mention_is_not_guessed(self):
        """@ 被渲染成 <@!openid> 占位符时明文会少一个，同样不记。"""
        got = bot.learn_display_names(GROUP, [ALICE, BOB], "<@!%s> @老莫 来一下" % ALICE)
        self.assertEqual(got, 0)
        self.assertIsNone(bot.DISPLAY_NAMES.of(GROUP, BOB))

    def test_it_binds_to_the_mentioned_person_not_the_sender(self):
        """@ 的明文是**被 @ 那个人**的昵称，不是发言人的。"""
        bot.learn_display_names(GROUP, [BOB], "喂，@老莫 在吗")
        # 发言人（ALICE）不该被挂上「老莫」
        self.assertIsNone(bot.DISPLAY_NAMES.of(GROUP, ALICE))
        self.assertEqual(bot.DISPLAY_NAMES.of(GROUP, BOB), "老莫")

    def test_nobody_mentioned_learns_nothing(self):
        self.assertEqual(bot.learn_display_names(GROUP, [], "@奶龙 在吗"), 0)

    def test_call_site_never_passes_the_sender(self):
        """源码级守卫：喂进去的必须是**被 @ 的人**，不是发言人。

        把 @ 的明文挂到发言人头上会全盘皆错 —— 大部分 @ 都是 @ 别人的。
        """
        calls = re.findall(r"(?<!def )learn_display_names\(([^)]*)\)",
                           inspect.getsource(bot), re.S)
        self.assertTrue(calls, "找不到调用点，这条守卫就失去意义了")
        for call in calls:
            self.assertIn("mentioned_others", call)
            self.assertNotIn("author", call)
            self.assertNotIn("sender", call)


class DisplayFallbackTest(unittest.TestCase):
    """显示优先级：认领过的称呼 > 群里挂的显示名 > 泛称。"""

    def setUp(self):
        self.saved = bot.DISPLAY_NAMES
        bot.DISPLAY_NAMES = relations.DisplayNames()
        bot.DISPLAY_NAMES.learn(GROUP, BOB, "贵阳老莫")

    def tearDown(self):
        bot.DISPLAY_NAMES = self.saved

    def test_claimed_name_wins_over_display_name(self):
        bot.RELATIONS.set_nick(GROUP, BOB, "罗老板", source="owner")
        try:
            label = bot.mention_label_for(GROUP)(BOB)
        finally:
            bot.RELATIONS.records.pop(f"{GROUP}|{BOB}", None)
        self.assertEqual(label, "罗老板", "认领过的称呼必须优先")

    def test_falls_back_to_the_display_name(self):
        self.assertEqual(bot.mention_label_for(GROUP)(BOB), "贵阳老莫")

    def test_unknown_person_still_returns_none(self):
        self.assertIsNone(bot.mention_label_for(GROUP)("OPENID_DN_NOBODY"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
