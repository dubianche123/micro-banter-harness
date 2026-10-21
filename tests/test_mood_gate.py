# -*- coding: utf-8 -*-
"""心情闸门与自动发疯（2026-10-22 群主：他说他很烦，一句猫娘模式就进去了，违和）。

两个系统此前互不感知：mood 只注入 AI 对话的语气提示词，模式切换是本地纯文案，
欢天喜地的进场词紧跟在「他今天有点烦」后面 —— 割裂感就是这么来的。

修法两条：
  ① 手动路径加心情门槛（mood_allows_mode）：元气/玩闹类（catgirl/driver）心情差
     演不了；crazy 不设 —— 烦的时候发疯反而最真。只拦进入，退出永远畅通。
  ② 自动路径（_auto_mood_targets）：心情 ≤ 发疯线持续够久 → 自动进 crazy，
     回升 → 自动退。只管自己自动进的，手动进的归手动管；三闸防抖。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "..")

import runtime
from storage import StateStore

import bot
import config

GROUP = "TEST-MOOD-GATE-GROUP"


class MoodAllowsModeTest(unittest.TestCase):

    def test_energetic_modes_blocked_when_grumpy(self):
        self.assertFalse(bot.mood_allows_mode("catgirl", -0.6),
                         "很烦的时候演不了元气猫娘")
        self.assertFalse(bot.mood_allows_mode("driver", -0.6))

    def test_energetic_modes_pass_when_mood_ok(self):
        self.assertTrue(bot.mood_allows_mode("catgirl", -0.4))
        self.assertTrue(bot.mood_allows_mode("catgirl", 0.8))

    def test_crazy_has_no_floor_by_design(self):
        """烦的时候发疯反而最真 —— crazy 不设门槛是特性不是遗漏。"""
        self.assertTrue(bot.mood_allows_mode("crazy", -0.95))

    def test_unlisted_modes_always_pass(self):
        for mode in ("discipline", "fortune"):
            self.assertTrue(bot.mood_allows_mode(mode, -0.9))


class AutoMoodTargetsTest(unittest.TestCase):
    """自动发疯：进入三闸（持续时长/每日上限/退出线）与 origin 归属。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(str(Path(self.tmp.name) / "state.json"))
        self._saved_state = runtime.STATE
        self._saved_buffers = dict(bot.group_buffers)
        runtime.STATE = self.state
        bot.group_buffers.clear()
        self.now = time.time()

    def tearDown(self):
        runtime.STATE = self._saved_state
        bot.group_buffers.clear()
        bot.group_buffers.update(self._saved_buffers)
        self.tmp.cleanup()

    def _moods(self, monkeypatch_value):
        return patch.object(bot.activation, "current_mood", return_value=monkeypatch_value)

    def test_first_low_mood_only_records_timestamp(self):
        bot.group_buffers[GROUP] = [{"text": "?", "time": self.now}]
        with self._moods(-0.8):
            self.assertEqual(bot._auto_mood_targets(self.now), [])
        slot = self.state.data["groups"][GROUP]
        self.assertEqual(slot["low_mood_since"], self.now)
        self.assertEqual(self.state.data["modes"].get(GROUP), None)

    def test_low_mood_long_enough_triggers_enter(self):
        bot.group_buffers[GROUP] = [{"text": "?", "time": self.now}]
        self.state.data["groups"][GROUP] = {"low_mood_since": self.now - 31 * 60}
        with self._moods(-0.8):
            self.assertEqual(bot._auto_mood_targets(self.now), [(GROUP, "enter")])
        slot = self.state.data["groups"][GROUP]
        self.assertEqual(slot["auto_mood_count"], 1)
        self.assertEqual(slot["low_mood_since"], 0.0)

    def test_short_grump_does_not_explode(self):
        """烦一下就疯等于一点就炸 —— 持续时长闸。"""
        bot.group_buffers[GROUP] = [{"text": "?", "time": self.now}]
        self.state.data["groups"][GROUP] = {"low_mood_since": self.now - 5 * 60}
        with self._moods(-0.8):
            self.assertEqual(bot._auto_mood_targets(self.now), [])

    def test_daily_cap_stops_the_tantrum_loop(self):
        bot.group_buffers[GROUP] = [{"text": "?", "time": self.now}]
        self.state.data["groups"][GROUP] = {
            "low_mood_since": self.now - 31 * 60,
            "auto_mood_day": time.strftime("%Y-%m-%d", time.localtime(self.now)),
            "auto_mood_count": config.AUTO_MODE_DAILY_CAP,
        }
        with self._moods(-0.8):
            self.assertEqual(bot._auto_mood_targets(self.now), [])

    def test_recovered_mood_exits_only_auto_origin(self):
        """退出只认自己自动进的；手动进的 crazy 归手动管。"""
        self.state.data["modes"][GROUP] = "crazy"
        self.state.data["groups"][GROUP] = {"auto_mode_origin": "auto"}
        with self._moods(-0.1):
            self.assertEqual(bot._auto_mood_targets(self.now), [(GROUP, "exit")])
        # 手动进的（无 origin 标记）：心情再好也不碰
        self.state.data["groups"][GROUP] = {}
        with self._moods(0.5):
            self.assertEqual(bot._auto_mood_targets(self.now), [])

    def test_mood_bounce_clears_the_timer(self):
        """回到发疯线上方就清计时 —— 别让很久前的一次低落永久挂着。"""
        bot.group_buffers[GROUP] = [{"text": "?", "time": self.now}]
        self.state.data["groups"][GROUP] = {"low_mood_since": self.now - 60 * 60}
        with self._moods(-0.5):
            self.assertEqual(bot._auto_mood_targets(self.now), [])
        self.assertEqual(self.state.data["groups"][GROUP]["low_mood_since"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
