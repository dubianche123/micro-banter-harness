# -*- coding: utf-8 -*-
"""克制版读图的回归测试（2026-10-19 群主定调）。

架构：群友发图 → glm-4v-flash（这把 key 免费）一句话描述 → 描述替换 [图片] 标记
进背景缓存，**读一次永久复用**。克制三闸：60s 冷却 / 每日 30 张 / 只在要回复或
闸门放行时读。描述是机器写的客观短句，进背景后受引用封顶和乱码检测的既有管辖。
"""
import sys
import time
import unittest

sys.path.insert(0, "..")

import providers  # noqa: E402

import bot  # noqa: E402

GROUP = "TESTGROUP_VISION"
OID = "TESTUSER_VISION00000000000000000"


class VisionGateTest(unittest.TestCase):

    def setUp(self):
        bot._vision_state.update({"last": 0.0, "day": "2026-10-19", "count": 0})
        bot._caption_cache.clear()

    def tearDown(self):
        bot._vision_state.update({"last": 0.0, "day": "2026-10-19", "count": 0})
        bot._caption_cache.clear()

    def test_cooldown_blocks_immediate_second_read(self):
        now = time.time()
        self.assertTrue(bot._vision_gate(now))
        self.assertFalse(bot._vision_gate(now + 1), "冷却期内不该再放行")
        self.assertTrue(bot._vision_gate(now + 61), "冷却过了就放行")

    def test_daily_cap_stops_the_firehose(self):
        now = time.time()
        for _ in range(bot.config.VISION_DAILY_CAP + 5):
            ok = bot._vision_gate(now)
        self.assertFalse(ok, "超过每日上限后不再放行")

    def test_disabled_flag_blocks_everything(self):
        bot.config.VISION_ENABLED = False
        try:
            self.assertFalse(bot._vision_gate(time.time()))
            self.assertFalse(bot._vision_budget_ok(time.time()))
        finally:
            bot.config.VISION_ENABLED = True

    def test_budget_survives_day_rollover(self):
        bot._vision_state["count"] = bot.config.VISION_DAILY_CAP
        self.assertFalse(bot._vision_budget_ok(time.time()))
        bot._vision_state["day"] = "2000-01-01"      # 跨天：上限重置
        self.assertTrue(bot._vision_budget_ok(time.time()))


class CaptionIntoBufferTest(unittest.TestCase):

    def setUp(self):
        bot._vision_state.update({"last": 0.0, "day": "2026-10-19", "count": 0})
        bot._caption_cache.clear()
        self.saved_buf = dict(bot.group_buffers)
        self._saved_describe = providers.describe_image
        bot.group_buffers[GROUP] = [
            {"sender": OID, "text": "[图片]", "time": time.time()},
            {"sender": OID, "text": "?", "time": time.time()},
        ]

    def tearDown(self):
        bot.group_buffers.clear()
        bot.group_buffers.update(self.saved_buf)
        bot._caption_cache.clear()
        providers.describe_image = self._saved_describe

    def test_caption_replaces_the_marker(self):
        async def fake(url):
            return "一只猫趴在键盘上"
        providers.describe_image = fake
        entry_time = bot.group_buffers[GROUP][0]["time"]

        asyncio_run(bot._caption_into_buffer(GROUP, "https://img.example/1", entry_time))

        self.assertEqual(bot.group_buffers[GROUP][0]["text"], "[图片：一只猫趴在键盘上]")
        self.assertEqual(bot._caption_cache["https://img.example/1"], "一只猫趴在键盘上")

    def test_failed_caption_keeps_bare_marker(self):
        async def fake(url):
            return None
        providers.describe_image = fake
        entry_time = bot.group_buffers[GROUP][0]["time"]

        asyncio_run(bot._caption_into_buffer(GROUP, "https://img.example/2", entry_time))

        self.assertEqual(bot.group_buffers[GROUP][0]["text"], "[图片]")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
