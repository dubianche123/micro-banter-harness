"""群主指代识别回归测试。

这块的需求是「群友提到群主时更容易被接话」，而实现的关键约束是
**不许写死具体人名** —— 机器人要开源，换个群、群主改个名都得开箱能用。
所以指代只能来自三个来源：通用词、群主自己认领的称呼、以及直接 @ 他。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot
import config

GROUP = "TESTGROUP_OWNERMENTION"
OWNER = "OWNER_OPENID_FOR_TEST"
OTHER = "OTHER_OPENID_FOR_TEST"


class OwnerReferenceTest(unittest.TestCase):

    def setUp(self):
        self._saved_owner = bot.OWNER_OPENID
        bot.OWNER_OPENID = OWNER
        # 只动内存里的档案，不调 mark_dirty，免得把测试数据刷进 state.json
        bot.RELATIONS.get(GROUP, OWNER)["nick"] = None
        bot.RELATIONS.get(GROUP, OTHER)["nick"] = "阿强"

    def tearDown(self):
        bot.OWNER_OPENID = self._saved_owner
        for k in [k for k in bot.RELATIONS.records if k.startswith(GROUP)]:
            bot.RELATIONS.records.pop(k, None)

    def test_generic_terms_always_present(self):
        """群主还没认领称呼时，靠通用词仍然认得出来。"""
        terms = bot.owner_reference_terms(GROUP)
        self.assertIn("群主", terms)
        self.assertIn("群管", terms)

    def test_claimed_nick_joins_terms(self):
        """群主说一句「我是老张」，之后「老张」就自动算指代他。"""
        self.assertNotIn("老张", bot.owner_reference_terms(GROUP))
        bot.RELATIONS.get(GROUP, OWNER)["nick"] = "老张"
        self.assertIn("老张", bot.owner_reference_terms(GROUP))

    def test_no_owner_openid_does_not_crash(self):
        """owner 还没识别出来（启动早期）也不能炸。"""
        bot.OWNER_OPENID = None
        self.assertEqual(bot.owner_reference_terms(GROUP), {"群主", "群管"})
        self.assertTrue(bot.mentions_owner("群主呢", GROUP))

    def test_detects_generic_mention(self):
        self.assertTrue(bot.mentions_owner("群主今天来不来", GROUP))
        self.assertTrue(bot.mentions_owner("群管在吗", GROUP))

    def test_detects_claimed_nick_mention(self):
        bot.RELATIONS.get(GROUP, OWNER)["nick"] = "老张"
        self.assertTrue(bot.mentions_owner("老张又加班了吧", GROUP))

    def test_detects_direct_at(self):
        """@ 群主时正文里可能一个字都没提他，只能靠 openid 认。"""
        self.assertTrue(bot.mentions_owner("今天天气不错", GROUP, [OWNER]))

    def test_plain_chat_not_matched(self):
        for text in ("这游戏好玩", "有人打排位吗", "笑死", ""):
            with self.subTest(text=text):
                self.assertFalse(bot.mentions_owner(text, GROUP))

    def test_other_members_nick_not_matched(self):
        """别人认领的名字不能被当成群主 —— 那会让插嘴变成随机事件。"""
        self.assertFalse(bot.mentions_owner("阿强来了", GROUP))
        self.assertFalse(bot.mentions_owner("阿强来了", GROUP, [OTHER]))


class OwnerProbabilityTest(unittest.TestCase):

    def test_owner_mention_is_more_likely_than_plain_chat(self):
        """这个功能的全部意义就在这个不等式上：说到群主该更容易被接话。"""
        self.assertGreater(config.OWNER_MENTION_PROBABILITY, config.BANTER_PROBABILITY)

    def test_probability_is_sane(self):
        self.assertGreater(config.OWNER_MENTION_PROBABILITY, 0.0)
        self.assertLessEqual(config.OWNER_MENTION_PROBABILITY, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
