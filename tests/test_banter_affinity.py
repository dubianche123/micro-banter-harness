# -*- coding: utf-8 -*-
"""亲密度影响「接谁的话」：越熟越容易被点着（2026-09-21 群主提的机制）。

设计要点：好感度调的是**权重**不是概率本身 —— 基数 config.BANTER_PROBABILITY 不动，
所以群里整体话痨程度不变，变的只是「更愿意接谁」。familiar 档权重定为 1.0，
因为大多数人落在这档；没档案的新人也是 1.0（不能因为还不熟就被晾着，那是把机制用成排斥）。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot
import relations


def _rec(score, interactions=10):
    return {"score": score, "interactions": interactions, "nick": None,
            "first_seen": 0, "last_seen": 0}


class WeightOrderingTest(unittest.TestCase):
    def test_closer_means_more_likely(self):
        order = ("blacklist", "cold", "distant", "familiar",
                 "acquaintance", "friend", "buddy", "soulmate")
        weights = [relations.BANTER_WEIGHTS[k] for k in order]
        self.assertEqual(weights, sorted(weights), "档位越高权重必须越大")

    def test_familiar_is_the_neutral_point(self):
        """基数不变的那档：多数人在这里，整体话量才不会漂。"""
        self.assertEqual(relations.BANTER_WEIGHTS["familiar"], 1.0)

    def test_score_maps_to_the_right_weight(self):
        self.assertEqual(relations.banter_weight(_rec(100)), 1.6)   # soulmate
        self.assertEqual(relations.banter_weight(_rec(-6)), 0.7)    # distant
        self.assertEqual(relations.banter_weight(_rec(-80)), 0.3)   # blacklist


class StrangerIsNotPunishedTest(unittest.TestCase):
    def test_no_record_is_neutral(self):
        self.assertEqual(relations.banter_weight(None), 1.0)
        self.assertEqual(relations.banter_weight({}), 1.0)

    def test_never_interacted_is_neutral(self):
        """留了档但没说过话（比如刚认领了名字）也按中性算。"""
        self.assertEqual(relations.banter_weight(_rec(0, interactions=0)), 1.0)


class ChanceScalingTest(unittest.TestCase):
    BASE = 0.08

    def test_close_person_gets_a_boost(self):
        chance = bot.banter_chance(self.BASE, _rec(100))
        self.assertAlmostEqual(chance, 0.128, places=4)

    def test_distant_person_gets_damped(self):
        self.assertAlmostEqual(bot.banter_chance(self.BASE, _rec(-6)), 0.056, places=4)

    def test_stranger_keeps_the_base_rate(self):
        self.assertAlmostEqual(bot.banter_chance(self.BASE, None), self.BASE, places=6)

    def test_ceiling_holds_even_if_base_is_raised(self):
        """基数被调得再大，也不能把谁变成刷屏。"""
        self.assertLessEqual(bot.banter_chance(0.9, _rec(100)), 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
