# -*- coding: utf-8 -*-
"""语气规则回归：攻击性收一收，幽默往前提（2026-09-20 群友实测反馈）。

实战样本（智谱顶班时段）：「那你先去舔个门把手试试」「你喜欢啥自己心里没数吗」
—— 连续怼脸、损完不给台阶，群友点名「攻击性有点强」。修法是在 PROMPT_NORMAL
的【说话方式】里把「好玩」排到「厉害」前面、给损人上分寸。这里钉住别改回去。
⚠️ 本文件只做描述性断言，不往提示词里写例句（例句会被当模板抄，见 test_digest_names）。
"""
import sys
import unittest

sys.path.insert(0, "..")

import prompts


class NormalModeToneTest(unittest.TestCase):
    def test_humor_comes_before_winning(self):
        self.assertIn("好玩永远排在厉害前面", prompts.PROMPT_NORMAL)

    def test_roast_has_restraint(self):
        self.assertIn("损一句就收", prompts.PROMPT_NORMAL)
        self.assertIn("给对方留台阶", prompts.PROMPT_NORMAL)
        self.assertIn("绝不追着打第二轮", prompts.PROMPT_NORMAL)

    def test_backpedal_when_the_other_side_disengages(self):
        self.assertIn("立刻收", prompts.PROMPT_NORMAL)

    def test_blanket_roast_license_is_gone(self):
        """旧版把「损人」和吐槽抬杠并列成无差别许可 —— 那是攻击性过强的源头。"""
        self.assertNotIn("可以吐槽、抬杠、损人，但不下流", prompts.PROMPT_NORMAL)


class BanterNoteTest(unittest.TestCase):
    """插嘴不是回最新一条：窗口里任何一句/整体场面都能成为插嘴由头（2026-09-20 群主要求）。

    旧文案「潜水刷到了上面这句话」把插嘴框死在触发随机的那一条上；实际上插嘴轮里
    模型能看到【群聊最近背景】（最近几句、带发言人），它想接哪句都行。
    """

    def test_any_context_line_is_a_valid_target(self):
        self.assertIn("【群聊最近背景】里列的任何一句", prompts.PROMPT_BANTER_NOTE)
        self.assertIn("不管是谁说的", prompts.PROMPT_BANTER_NOTE)

    def test_whole_scene_counts_too(self):
        self.assertIn("场面", prompts.PROMPT_BANTER_NOTE)
        self.assertIn("气氛", prompts.PROMPT_BANTER_NOTE)

    def test_banter_mark_unchanged(self):
        """BANTER_MARK 是缓存分界与测试锚点，改措辞不许动它。"""
        self.assertEqual(prompts.BANTER_MARK, "【当前动作")


class QuoteOnceKeepItShortTest(unittest.TestCase):
    """2026-10-06 实测：龙少随口一句在 23 秒内被原样引用两遍、「硬菜」连吃三轮
    回合、对全场的审判框架一天用了四次 —— 旧规则管「点」，管不住「引用」。"""

    def test_quoting_others_is_capped_once(self):
        self.assertIn("别人的原话：一晚上最多引用一次", prompts.PROMPT_SHARED_RULES)
        self.assertIn("第二次出现在你嘴里就是复读", prompts.PROMPT_SHARED_RULES)

    def test_no_recap_tail_by_default(self):
        """Gemini 的结构性碎嘴：正文说完，再用「不过说真的」起头把旧话捋一遍。
        09-21 起的日志里这个尾巴就没断过——是模型体质，得用结构规则卡。"""
        self.assertIn("默认不写回忆尾巴", prompts.PROMPT_NORMAL)
        self.assertIn("放在开头当由头", prompts.PROMPT_NORMAL)

    def test_length_is_a_number_not_a_vibe(self):
        """「短」这种抽象要求实测会被无视，得给数字。"""
        self.assertIn("顶多三句", prompts.PROMPT_NORMAL)
        self.assertIn("不分段", prompts.PROMPT_NORMAL)

    def test_no_group_wide_verdict(self):
        # 合并后归入共享块【一个料只用一次】—— 所有「料」同一条总原则
        self.assertIn("全场审判", prompts.PROMPT_SHARED_RULES)
        self.assertIn("一天最多一次", prompts.PROMPT_SHARED_RULES)
        self.assertIn("接住那一个人就行", prompts.PROMPT_SHARED_RULES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
