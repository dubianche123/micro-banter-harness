# -*- coding: utf-8 -*-
"""亲近不能只剩损 + 承诺不当万能收尾句（2026-09-23 群主实测反馈）。

同一天暴露的两件事：
  1. 对本命档的人，输出里一句「护」都没有，全程单向挑刺（原话：「与其说关系好，
     不如说是单纯的损我」）。证据：对方说「小王辛苦了」，回的是「辛苦什么啊……
     下次麻烦你把服务器拔了」—— 善意被抬杠接住了。
  2. 「还没兑现：罗老板——请全群吃酸菜蹄髈」每轮随摘要灌进上下文，当天十几条
     回复里反复出现、收尾雷同；另外「你在旁边看戏」一晚上连着收尾 6 次。
"""
import sys
import unittest

sys.path.insert(0, "..")

import digest
import prompts
import relations


class OwnerNoteTest(unittest.TestCase):
    """群主是群里跟它最熟的人 —— 但「熟」不许被写成「可以随便怼」。"""

    def test_kindness_must_be_received_not_roasted(self):
        self.assertIn("先把那句好意接住", prompts.PROMPT_OWNER_NOTE,
                      "善意被抬杠接住是最伤的一种")

    def test_a_turn_cannot_be_only_jabs(self):
        self.assertIn("一段话里不能只有挑刺", prompts.PROMPT_OWNER_NOTE)

    def test_stands_by_him_when_he_is_having_a_bad_time(self):
        self.assertIn("第一反应是护着他", prompts.PROMPT_OWNER_NOTE)

    def test_answers_what_he_actually_said(self):
        """别把对方的话当跳板、一开口就拐到别人身上。"""
        self.assertIn("先接他说的这件事", prompts.PROMPT_OWNER_NOTE)


class CloseTierKeepsTeasingInBoundTest(unittest.TestCase):
    def test_top_tier_caps_the_teasing(self):
        self.assertIn("损只能是整段里的一句", relations.LEVEL_HINTS["soulmate"])

    def test_top_tier_forbids_roasting_kindness(self):
        self.assertIn("好意", relations.LEVEL_HINTS["soulmate"])

    def test_close_tiers_are_instructed_to_take_his_side(self):
        self.assertIn("站他那边的", relations.LEVEL_HINTS["friend"])
        self.assertIn("站他那边", relations.LEVEL_HINTS["buddy"])


class NoRunningGagEndingTest(unittest.TestCase):
    def test_a_gripe_counts_as_a_prop(self):
        """光管比喻不管槽点的话，「看戏」照样刷屏。"""
        self.assertIn("吐槽点也算道具", prompts.PROMPT_SHARED_RULES)

    def test_promises_are_not_a_sign_off(self):
        self.assertIn("固定结尾", prompts.PROMPT_MEMORY_HEADER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
