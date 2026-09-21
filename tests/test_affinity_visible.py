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


class InsultActuallyCostsAffinityTest(unittest.TestCase):
    """「小王我操你妈」连着说，好感度一分没掉 —— 词表里根本没有这类指向性辱骂。"""

    def test_directed_insults_score_negative(self):
        for text in ("小王我操你妈", "小王我想操你", "傻逼小王谁问你了", "你去死吧",
                     "小王你妈的"):
            self.assertEqual(relations.local_sentiment(text), -1, text)

    def test_plain_exclamations_are_not_punished(self):
        """「我操了」「卧槽」是语气词不是骂人 —— 冤案比漏判更伤。"""
        for text in ("我操了", "卧槽这也行", "我今天真累", ""):
            self.assertEqual(relations.local_sentiment(text), 0, text)

    def test_positive_words_still_win(self):
        self.assertEqual(relations.local_sentiment("谢谢小王，太厉害了"), 1)


class MilestoneIsAnnouncedTest(unittest.TestCase):
    """跨了 10 分的台阶要被播报 —— 但只给感觉，不给数字。"""

    # 现在写的是分数（relations.apply：new_score // 10 != old_score // 10 就记一次）
    REC = {
        "score": 12, "nick": "阿强", "interactions": 20,
        "last_seen": 0, "first_seen": 0,
        "pending_milestone": {"from": 7, "to": 12, "ts": 0},
    }

    def test_legacy_tier_names_still_render(self):
        """老档里存的是档位名（"familiar" 这种），读的时候不能崩。"""
        rec = dict(self.REC,
                   pending_milestone={"from": "familiar", "to": "acquaintance", "ts": 0})
        note = relations.build_relation_note(rec, "some-openid")
        self.assertIn("更熟了", note)
        self.assertIn("刚刚的变化", note)

    def test_going_down_reads_as_drifted_apart(self):
        rec = dict(self.REC, pending_milestone={"from": 12, "to": 2, "ts": 0})
        self.assertIn("更生分了", relations.build_relation_note(rec, "some-openid"))

    def test_no_numbers_leak_into_the_note(self):
        """播报里一个数字都不许有 —— 分数是内部账本，群里只该听见「更熟了」。

        （提示词里出现的「好感度」是给模型下的禁令，不是要说出去的内容，所以只查数字。）
        """
        note = relations.build_relation_note(self.REC, "some-openid")
        line = [l for l in note.splitlines() if "刚刚的变化" in l][0]
        self.assertFalse(any(c.isdigit() for c in line), line)
        self.assertIn("熟人", line, "档位要用称谓说出来，不能光说「升了一档」")

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
