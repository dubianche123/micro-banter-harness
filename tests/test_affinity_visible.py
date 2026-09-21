# -*- coding: utf-8 -*-
"""好感度要「看得见」：档位决定语气分寸，跨档位时当场说出来。

2026-09-21 群主反馈：好感度系统没任何体现。查下来机制其实齐全（8 档、按模式换叫法、
有里程碑），问题是两处太软：
1. LEVEL_HINTS 每档只有一句抽象描述（「不太熟」），模型基本不看，八档一个味儿；
2. 里程碑那段写的是「自然地流露一下」，模型经常一句都不提 —— 档案里档位变了，群里没人知道。
这里把两条都钉住。
"""
import sys
import unittest

sys.path.insert(0, "..")

import relations


class LevelHintsAreActionableTest(unittest.TestCase):
    def test_every_level_has_a_concrete_instruction(self):
        """每档至少带一条具体行为（能做什么 / 不许做什么），不能只是抽象形容词。"""
        for key, hint in relations.LEVEL_HINTS.items():
            self.assertTrue(len(hint) >= 12, f"{key} 档的提示太短，模型不会当回事：{hint}")

    def test_close_levels_may_tease_openly(self):
        """熟到一定程度就该敢损敢护 —— 这是「熟」最直观的体感。"""
        for key in ("friend", "buddy"):
            self.assertIn("损", relations.LEVEL_HINTS[key])
        self.assertIn("偏心护短", relations.LEVEL_HINTS["soulmate"])

    def test_distant_levels_keep_distance(self):
        self.assertIn("别套近乎", relations.LEVEL_HINTS["distant"])
        self.assertIn("老相识", relations.LEVEL_HINTS["familiar"])

    def test_hints_are_not_all_the_same(self):
        """八档要是写出八句差不多的话，等于没分档。"""
        self.assertEqual(len(set(relations.LEVEL_HINTS.values())), len(relations.LEVEL_HINTS))


class MilestoneIsAnnouncedTest(unittest.TestCase):
    REC = {
        "score": 12, "nick": "阿强", "interactions": 20,
        "last_seen": 0, "first_seen": 0,
        "pending_milestone": {"from": "familiar", "to": "acquaintance", "ts": 0},
    }

    def test_note_demands_saying_it_out_loud(self):
        note = relations.build_relation_note(self.REC, "some-openid")
        self.assertIn("刚刚的变化", note)
        self.assertIn("要当场说出来", note, "只写「流露一下」模型会一个字不提")

    def test_note_says_do_not_keep_repeating(self):
        note = relations.build_relation_note(self.REC, "some-openid")
        self.assertIn("翻篇", note)

    def test_no_milestone_means_no_such_line(self):
        rec = dict(self.REC, pending_milestone=None)
        self.assertNotIn("刚刚的变化", relations.build_relation_note(rec, "some-openid"))


class ToneTierOverridesPersonaTest(unittest.TestCase):
    def test_note_tells_model_the_tier_wins(self):
        rec = {"score": 90, "nick": "老王", "interactions": 50, "last_seen": 0, "first_seen": 0}
        note = relations.build_relation_note(rec, "some-openid")
        self.assertIn("优先级高于人设里的默认语气", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
