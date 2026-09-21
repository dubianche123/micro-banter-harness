# -*- coding: utf-8 -*-
"""模式切换的交情门槛（2026-09-21 群主反馈：家豪 −6 分照样切猫娘）。

模式是**整群的公共状态**，不能让一个跟小王还没处熟（甚至一直在骂它）的人一句话按走。
所以切换前先过 mode_switch_allowed：太生分的回绝，熟人照旧。

⚠️ 门槛是防「处得不好还来使唤」，不是防新人 —— 没打过交道的人一律放行，
   否则机制就变成了「新人什么都不许玩」的排斥。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot
import config

GROUP = "TEST-MODE-GATE-GROUP"
SOMEONE = "OID-SOMEONE-0001"


def _seed(score, interactions=20):
    """把某人摆到指定好感度上；interactions=0 表示还没打过交道。"""
    rec = bot.RELATIONS.get(GROUP, SOMEONE)
    rec["score"] = score
    rec["interactions"] = interactions
    return rec


class GateTest(unittest.TestCase):
    def setUp(self):
        for k in [k for k in bot.RELATIONS.records if k.startswith(GROUP)]:
            bot.RELATIONS.records.pop(k, None)

    def test_stranger_is_welcomed(self):
        """没档案的人不该被门槛挡在门外。"""
        for k in [k for k in bot.RELATIONS.records if k.startswith(GROUP)]:
            bot.RELATIONS.records.pop(k, None)
        self.assertTrue(bot.mode_switch_allowed(GROUP, SOMEONE))

    def test_never_interacted_is_welcomed(self):
        """留了档但还没说过话（比如刚认领名字）也算新人。"""
        _seed(0, interactions=0)
        self.assertTrue(bot.mode_switch_allowed(GROUP, SOMEONE))

    def test_low_affinity_is_refused(self):
        """家豪那种一直骂人的（−6，distant 档）必须被拦。"""
        _seed(-6)
        self.assertFalse(bot.mode_switch_allowed(GROUP, SOMEONE))

    def test_cold_and_blacklist_refused(self):
        for score in (-40, -100):
            _seed(score)
            self.assertFalse(bot.mode_switch_allowed(GROUP, SOMEONE), score)

    def test_threshold_is_the_familiar_line(self):
        """门槛默认压在 familiar 档：刚好及格和差一分，待遇不同。"""
        _seed(config.MODE_SWITCH_MIN_AFFINITY)
        self.assertTrue(bot.mode_switch_allowed(GROUP, SOMEONE))
        _seed(config.MODE_SWITCH_MIN_AFFINITY - 1)
        self.assertFalse(bot.mode_switch_allowed(GROUP, SOMEONE))

    def test_close_friends_pass(self):
        for score in (10, 30, 60, 100):
            _seed(score)
            self.assertTrue(bot.mode_switch_allowed(GROUP, SOMEONE), score)

    def test_owner_always_passes(self):
        """群主调试/演示要能随时切，哪怕他自己把好感度玩成负数。"""
        _seed(-100)
        self.assertTrue(bot.mode_switch_allowed(GROUP, SOMEONE, is_owner=True))

    def test_disabled_affinity_means_no_gate(self):
        old = config.AFFINITY_ENABLED
        try:
            config.AFFINITY_ENABLED = False
            _seed(-100)
            self.assertTrue(bot.mode_switch_allowed(GROUP, SOMEONE))
        finally:
            config.AFFINITY_ENABLED = old


class RefusalWordingTest(unittest.TestCase):
    """回绝的话不能把内部评分念出来。"""

    def test_no_numbers_and_no_score_words(self):
        for line in bot.MODE_DENIED_LINES:
            self.assertNotIn("好感度", line)
            self.assertNotIn("亲密度", line)
            self.assertFalse(any(c.isdigit() for c in line), line)

    def test_refusal_says_something(self):
        line = bot.mode_denied_line(GROUP)
        self.assertTrue(line.strip())
        self.assertIn(line, bot.MODE_DENIED_LINES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
