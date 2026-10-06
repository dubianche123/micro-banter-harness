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

    def test_maxed_out_promise_leaves_a_tombstone(self):
        """催满上限 → 撤下 + 留墓碑。没有墓碑的话，压缩 sync 会把它当新承诺
        复活（nag 清零）—— 实测罗老板一天被 @ 三次，第 3 次绕过了 max_nag=2。"""
        self.book.sync("g1", self.rows)
        pid = self.book.list("g1")[0]["id"]
        self.book.mark_nagged("g1", pid)
        self.book.mark_nagged("g1", pid)
        self.assertEqual(self.book.list("g1"), [], "催满上限的行要撤下")
        tombs = self.book.finished["g1"]
        self.assertEqual(len(tombs), 1)
        self.assertEqual(tombs[0]["who"], "罗老板")

    def test_sync_does_not_resurrect_a_finished_promise(self):
        """压缩模型看到群里还在聊这事，就会把它再写进摘要 —— sync 必须拦住。"""
        self.book.sync("g1", self.rows)
        pid = self.book.list("g1")[0]["id"]
        self.book.mark_nagged("g1", pid)
        self.book.mark_nagged("g1", pid)
        added, _ = self.book.sync("g1", [{"who": "罗老板", "what": "答应请全群吃酸菜蹄髈"}])
        self.assertEqual(added, 0, "催满的承诺改个措辞也不许复活")
        self.assertEqual(self.book.list("g1"), [])

    def test_genuinely_new_promise_still_lands(self):
        """墓碑只拦「同一件事」，谁换了新花样照常记账。"""
        self.book.sync("g1", self.rows)
        pid = self.book.list("g1")[0]["id"]
        self.book.mark_nagged("g1", pid)
        self.book.mark_nagged("g1", pid)
        added, _ = self.book.sync("g1", [{"who": "罗老板", "what": "下周请大家喝奶茶"}])
        self.assertEqual(added, 1, "真正的新承诺要正常入账")
        self.assertEqual(self.book.list("g1")[0]["what"], "下周请大家喝奶茶")

    def test_finished_persists_through_dump_and_hydrate(self):
        """墓碑要跨重启活着，不然重启一次压缩就把旧账全复活了。"""
        self.book.sync("g1", self.rows)
        pid = self.book.list("g1")[0]["id"]
        self.book.mark_nagged("g1", pid)
        self.book.mark_nagged("g1", pid)
        data = {}
        self.book.dump_into(data)
        book2 = digest.PromiseBook()
        book2.hydrate(data.get("promises", {}), finished=data.get("promises_finished", {}))
        added, _ = book2.sync("g1", [{"who": "罗老板", "what": "说要请全群吃酸菜蹄髈"}])
        self.assertEqual(added, 0, "重启后墓碑必须还在")

    def test_ledger_never_rides_the_per_turn_injection(self):
        """2026-10-06 对照实验后台账彻底退出常规注入（实验里它是回忆尾巴的最大引力源）：
        bot.py 不再渲染摘要正文，只喂名册；「take_for_prompt 过滤后陪跑」的旧接线随之拆除。

        台账的出口只剩两个独立入口：催债定时任务、查账触发词 —— 需要时查得到，
        不需要时不进上下文。这条测试反向钉住：注入相关代码里不许再把台账喂回去。"""
        import inspect

        import bot
        src = inspect.getsource(bot)
        self.assertNotIn("take_for_prompt", src,
                         "台账又回到常规注入里了")
        self.assertIn("render_people", src,
                      "名册注入（防张冠李戴）必须还在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
