# -*- coding: utf-8 -*-
"""承诺台账一天只在上下文里露一次（2026-09-23 群主反馈）。

事故：摘要里那行「🧾 还没兑现：罗老板——请全群吃酸菜蹄髈」是**每轮都灌进上下文**
的，结果当天十几条回复句句不离这四个字，收尾雷同 —— 群里看着就是揪着一桌菜不放。

催债本来就有 dun_promises 定时在做（隔 48 小时、最多 2 次），常态回复不需要天天
领这块料。所以台账层面加一道闸：同一条承诺当天露过一次就不再喂，次日重置。
⚠️「查账 / 欠我」这类显式查询走另一个入口 —— 人家开口问了，得如实报。
"""
import sys
import time
import unittest

sys.path.insert(0, "..")

import digest


class PromiseShownOnceADayTest(unittest.TestCase):
    def setUp(self):
        self.book = digest.PromiseBook()
        self.rows = [{"who": "罗老板", "what": "说要请全群吃酸菜蹄髈"}]

    def test_first_touch_of_the_day_shows_it(self):
        self.assertEqual(len(self.book.take_for_prompt("g1", self.rows)), 1)

    def test_second_touch_same_day_is_hidden(self):
        self.book.take_for_prompt("g1", self.rows)
        self.assertEqual(self.book.take_for_prompt("g1", self.rows), [])

    def test_next_day_it_is_back(self):
        """隔天可以再提一次 —— 是「别刷屏」，不是「永远不许提」。"""
        now = time.time()
        self.book.take_for_prompt("g1", self.rows, now=now)
        self.assertEqual(len(self.book.take_for_prompt("g1", self.rows, now=now + 86400)), 1)

    def test_another_promise_is_not_collateral_damage(self):
        """一条今天露过了，摘要里新冒出来的承诺不该跟着一起消失。"""
        self.book.take_for_prompt("g1", self.rows)
        rows = self.rows + [{"who": "原理", "what": "说要发资料"}]
        self.assertEqual([p["who"] for p in self.book.take_for_prompt("g1", rows)], ["原理"])

    def test_empty_rows_is_harmless(self):
        self.assertEqual(self.book.take_for_prompt("g1", None), [])
        self.assertEqual(self.book.take_for_prompt("g1", []), [])

    def test_explicit_queries_still_see_everything(self):
        """「查账」是人家开口问了，得如实报 —— 这道闸只拦自动注入。"""
        self.book.take_for_prompt("g1", self.rows)
        self.assertEqual(len(self.book.list("g1")), 1)

    def test_injection_wiring_filters_the_ledger(self):
        """闸装在库里却不接线，等于没做 —— 注入处必须真去调它。"""
        import inspect

        import bot
        self.assertIn("take_for_prompt", inspect.getsource(bot),
                      "bot.py 注入长期记忆时没过滤承诺台账")


if __name__ == "__main__":
    unittest.main(verbosity=2)
