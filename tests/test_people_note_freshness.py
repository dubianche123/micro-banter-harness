# -*- coding: utf-8 -*-
"""人物名册的时效闸：一条「近况」不能挂到天荒地老（2026-09-24 群主当面吐槽）。

事故：09-19 家豪被封号（真事），压缩把它写进人物名册「家豪：号刚解封就积极参与各种
测试」。名册是**每轮都注入模型**的（09-20 为了修「张冠李戴」加的），于是这条当天就过期的
标签天天在它眼前晃 —— 09-24 22:36 它还在说「你这号刚解封」，18 秒后罗老板群里吐槽
「这傻福怎么和封号过不去了」，老王让它闭嘴。

提示词里已经写够了（「旧事不自动续杯」「people 写近况不写标签」），摘要正文也确实干净了，
但名册没被覆盖到：模型每次压缩都把旧条目**原样抄回来**，它认不出「刚」已经过期。
所以这层用代码卡：条目带时间戳，**文本没变就不算更新**，老到一定天数直接不喂。
"""
import sys
import time
import unittest

sys.path.insert(0, "..")

import config
import digest
import relations  # noqa: F401  （与 bot 同目录，确保 import 路径一致）


def _people(people):
    """给 stamp_people 用的是 data 段（不是整个 state）。"""
    return {"people": people}


def _state(people):
    return {"data": _people(people)}


class StampTest(unittest.TestCase):
    """打时间戳的口径：换个说法才算有更新。"""

    def test_new_note_gets_fresh_timestamp(self):
        new = _people([{"who": "家豪", "note": "天天在群里整活"}])
        digest.stamp_people(None, new, now=1000.0)
        self.assertEqual(new["people"][0]["ts"], 1000.0)

    def test_unchanged_note_keeps_aging(self):
        """模型把旧条目原样抄回来 = 没有新消息，那它就该继续变老。"""
        old = _people([{"who": "家豪", "note": "天天整活", "ts": 500.0}])
        new = _people([{"who": "家豪", "note": "天天整活"}])
        digest.stamp_people(old, new, now=1000.0)
        self.assertEqual(new["people"][0]["ts"], 500.0)

    def test_reworded_note_counts_as_an_update(self):
        old = _people([{"who": "家豪", "note": "天天整活", "ts": 500.0}])
        new = _people([{"who": "家豪", "note": "天天整活，最近迷上钓鱼"}])
        digest.stamp_people(old, new, now=1000.0)
        self.assertEqual(new["people"][0]["ts"], 1000.0)

    def test_capital_keys_are_tolerated(self):
        """压缩产物出现过 "Who"/"Note"（大写），认不出就等于名册空着。"""
        old = _people([{"Who": "罗老板", "Note": "爱整活", "ts": 500.0}])
        new = _people([{"Who": "罗老板", "Note": "爱整活"}])
        digest.stamp_people(old, new, now=1000.0)
        self.assertEqual(new["people"][0]["ts"], 500.0)


class RenderExpiryTest(unittest.TestCase):
    """过期的不再进上下文。"""

    def test_stale_note_is_not_injected(self):
        now = time.time()
        old_ts = now - (config.PEOPLE_NOTE_MAX_AGE_DAYS + 1) * 86400
        state = _state([{"who": "家豪", "note": "号刚解封", "ts": old_ts}])
        self.assertEqual(digest.render_people(state, now=now), "")

    def test_fresh_note_still_shows(self):
        now = time.time()
        state = _state([{"who": "家豪", "note": "天天整活", "ts": now - 3600}])
        self.assertIn("天天整活", digest.render_people(state, now=now))

    def test_note_without_timestamp_is_kept(self):
        """老档里没有 ts 的条目别一刀切清空 —— 名册空着比过期更糟（会退回张冠李戴）。"""
        state = _state([{"who": "家豪", "note": "天天整活"}])
        self.assertIn("天天整活", digest.render_people(state))

    def test_only_the_stale_one_drops_out(self):
        """过期的只是那一条，别把整份名册连坐清空。"""
        now = time.time()
        stale = now - (config.PEOPLE_NOTE_MAX_AGE_DAYS + 1) * 86400
        state = _state([
            {"who": "家豪", "note": "号刚解封", "ts": stale},
            {"who": "罗老板", "note": "爱整活", "ts": now - 60},
        ])
        out = digest.render_people(state, now=now)
        self.assertNotIn("家豪", out)
        self.assertIn("罗老板", out)

    def test_default_window_is_sane(self):
        """名册的定位是「最近在干什么」，两天是上限不是永久档案。"""
        self.assertLessEqual(config.PEOPLE_NOTE_MAX_AGE_DAYS, 3.0)
        self.assertGreaterEqual(config.PEOPLE_NOTE_MAX_AGE_DAYS, 1.0)


class PromptWordingTest(unittest.TestCase):
    """光有代码闸不够，压缩时也该明确禁止写时效标签。"""

    def test_reduce_forbids_perishable_wording(self):
        self.assertIn("刚解封", digest.SYSTEM_REDUCE)
        self.assertIn("过期", digest.SYSTEM_REDUCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
