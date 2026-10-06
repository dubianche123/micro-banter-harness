# -*- coding: utf-8 -*-
"""好感度怎么加减分：模型判方向、代码定幅度、每天封顶（2026-10-06 群主定的口径）。

要解决的是「被 @ 且没扣分就 +1」这条 —— 它让好感度退化成**被搭理次数**：
实测一天 100 多次记账里 108 次 +1，连一直骂人的也在涨分。

新口径三句话：
  1. **被 @ 本身不给分**。被搭理不等于被喜欢。
  2. **模型只判方向**（-1/0/1），绝对幅度交给代码 + 每日封顶。
  3. 封顶**不对称**：加分放宽（关系本来就该越聊越近），扣分收紧（误判更伤人）。
"""
import sys
import time
import unittest

sys.path.insert(0, "..")

import bot
import config
import prompts
import relations

GROUP = "TEST-AFF-GROUP"
OID = "OID-AFF-0001"


class DirectionProtocolTest(unittest.TestCase):
    """模型那份协议：只判方向，且把两条易错标准写死。"""

    def test_only_three_values_allowed(self):
        self.assertIn("-1、0、1", prompts.CMD_PROTOCOL)
        self.assertNotIn("范围 -3 到 +3", prompts.CMD_PROTOCOL)

    def test_mentioning_is_not_credit(self):
        self.assertIn("被 @ 或被叫到名字，本身什么都不算", prompts.CMD_PROTOCOL)

    def test_discipline_is_not_hostility(self):
        """09-24 老王喊「小王闭嘴」被判 -1 —— 那是管教，不是讨厌。"""
        self.assertIn("管教不算敌意", prompts.CMD_PROTOCOL)

    def test_it_is_told_not_to_please_anyone(self):
        self.assertIn("别讨好谁", prompts.CMD_PROTOCOL)

    def test_protocol_is_on_by_default(self):
        self.assertTrue(config.CMD_PROTOCOL_ENABLED)


class DailyCapTest(unittest.TestCase):
    def setUp(self):
        bot.STATE.data.pop("affinity_daily", None)
        for k in [k for k in bot.RELATIONS.records if k.startswith(GROUP)]:
            bot.RELATIONS.records.pop(k, None)
        # 新建档案的 last_seen=0，会被「久未往来」的衰减当成陈年旧账扣分 ——
        # 那是另一条机制，测封顶时先把它排除掉。
        bot.RELATIONS.get(GROUP, OID)["last_seen"] = time.time()

    def test_gains_stop_at_the_cap(self):
        for _ in range(config.AFFINITY_DAILY_GAIN_CAP + 5):
            bot.apply_affinity_delta_capped(GROUP, OID, 1, "test")
        rec = bot.RELATIONS.get(GROUP, OID, create=False)
        self.assertLessEqual(rec["score"], config.AFFINITY_DAILY_GAIN_CAP)

    def test_losses_stop_at_the_cap(self):
        for _ in range(config.AFFINITY_DAILY_LOSS_CAP + 5):
            bot.apply_affinity_delta_capped(GROUP, OID, -1, "test")
        rec = bot.RELATIONS.get(GROUP, OID, create=False)
        self.assertGreaterEqual(rec["score"], -config.AFFINITY_DAILY_LOSS_CAP)

    def test_caps_are_asymmetric_on_purpose(self):
        """涨得快、跌得慢：一次误判的伤害要能靠几天聊回来。"""
        self.assertGreater(config.AFFINITY_DAILY_GAIN_CAP, config.AFFINITY_DAILY_LOSS_CAP)

    def test_zero_costs_nothing(self):
        self.assertIsNone(bot.apply_affinity_delta_capped(GROUP, OID, 0, "test"))
        b = bot.affinity_budget(GROUP, OID)
        self.assertEqual((b["gain"], b["loss"]), (0, 0))

    def test_budget_resets_next_day(self):
        bot.apply_affinity_delta_capped(GROUP, OID, 1, "test")
        tomorrow = time.time() + 86400
        b = bot.affinity_budget(GROUP, OID, now=tomorrow)
        self.assertEqual(b["gain"], 0)
        # 且新的一天真的能再加
        bot.apply_affinity_delta_capped(GROUP, OID, 1, "test", now=tomorrow)
        self.assertEqual(bot.affinity_budget(GROUP, OID, now=tomorrow)["gain"], 1)

    def test_zero_mention_gives_nothing(self):
        """没有模型判定时退回词表；「在吗」这种中性消息既不涨分也不建档。"""
        bot.apply_affinity_delta_capped(GROUP, OID, relations.local_sentiment("在吗"), "test")
        self.assertEqual(bot.RELATIONS.get(GROUP, OID, create=False)["score"], 0)

    def test_insult_still_costs_affinity(self):
        bot.apply_affinity_delta_capped(
            GROUP, OID, relations.local_sentiment("小王我操你妈"), "test")
        self.assertEqual(bot.RELATIONS.get(GROUP, OID, create=False)["score"], -1)

    def test_digest_settlement_respects_the_cap_too(self):
        """压缩结算一次能到 ±6，它要是绕过封顶，封顶就形同虚设。"""
        for _ in range(3):
            bot.apply_affinity_delta_capped(
                GROUP, OID, -2, "压缩结算", span=config.AFFINITY_DIGEST_SPAN, count=False)
        rec = bot.RELATIONS.get(GROUP, OID, create=False)
        self.assertGreaterEqual(rec["score"], -config.AFFINITY_DAILY_LOSS_CAP)
        self.assertEqual(rec["interactions"], 0, "压缩结算不该虚增搭话次数")


class MilestoneOnceADayTest(unittest.TestCase):
    """日上限 10 = 一天能跨一个台阶 ⇒ 播报必须限流，否则它天天说「我们更熟了」。"""

    def setUp(self):
        for k in [k for k in bot.RELATIONS.records if k.startswith("TEST-MS")]:
            bot.RELATIONS.records.pop(k, None)
        bot.RELATIONS.get("TEST-MS", OID)["last_seen"] = time.time()

    # ⚠️ 单次 apply 的幅度默认被 clamp 到 ±3（一次互动不该撬动太多），
    #    所以测试里用 span 放大，才能一次跨过 10 分台阶。
    SPAN = config.AFFINITY_DIGEST_SPAN

    def _to_eight(self, now):
        r = bot.RELATIONS
        r.apply("TEST-MS", OID, 6, now=now, span=self.SPAN)   # 0 → 6
        r.apply("TEST-MS", OID, 2, now=now)                   # 6 → 8
        rec = r.get("TEST-MS", OID, create=False)
        rec["pending_milestone"] = None
        return rec

    def test_crossing_a_step_does_announce(self):
        rec = self._to_eight(time.time())
        bot.RELATIONS.apply("TEST-MS", OID, 6, span=self.SPAN)   # 8 → 14，跨过 10
        self.assertIsNotNone(rec.get("pending_milestone"))

    def test_second_crossing_in_a_day_is_not_announced(self):
        now = time.time()
        rec = self._to_eight(now)
        rec["told_day"] = time.strftime("%Y-%m-%d", time.localtime(now))
        bot.RELATIONS.apply("TEST-MS", OID, 6, span=self.SPAN)
        self.assertIsNone(rec.get("pending_milestone"), "同一天不该再排一次播报")

    def test_next_day_can_announce_again(self):
        now = time.time()
        rec = self._to_eight(now)
        rec["told_day"] = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
        bot.RELATIONS.apply("TEST-MS", OID, 6, span=self.SPAN, now=now + 86400)
        self.assertIsNotNone(rec.get("pending_milestone"))

    def test_the_score_still_moves_even_when_not_announced(self):
        """限流的是「说出来」，不是记账 —— 分该涨还得涨。"""
        rec = self._to_eight(time.time())
        rec["told_day"] = time.strftime("%Y-%m-%d")
        score = bot.RELATIONS.apply("TEST-MS", OID, 6, span=self.SPAN)[0]
        self.assertEqual(score, 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
