# -*- coding: utf-8 -*-
"""兜底文案池的回归测试（2026-10-15 群主看穿）。

那晚两条「没看清/再说一遍」不是模型装瞎，是 random.choice(FALLBACK_REPLIES)
在网络抖动期连抽了两条同款——池子里一半的句子都在**许诺复读**，而兜底触发时
模型根本没有应答，许诺必然落空。修复：池子重写（保留装瞎风味、去掉复读许诺）、
不放回连抽（相邻两条必不同款）。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402


class FallbackPoolTest(unittest.TestCase):

    def test_pool_never_promises_a_repeat(self):
        """兜底触发时模型没有应答——任何「再说一遍 / 再艾特我一次」都是空头支票。"""
        for t in bot.FALLBACK_REPLIES:
            self.assertNotIn("再说一遍", t)
            self.assertNotIn("再说一次", t)
            self.assertNotIn("再艾特", t)

    def test_pool_has_variety(self):
        """池子至少 4 条不重复——两条兜底连发时得能换着来。"""
        self.assertGreaterEqual(len(set(bot.FALLBACK_REPLIES)), 4)

    def test_never_the_same_line_twice_in_a_row(self):
        """不放回连抽：相邻两次兜底必不同款。"""
        prev = None
        for _ in range(30):
            t = bot._pick_fallback()
            self.assertNotEqual(t, prev, "相邻两条兜底不该是同一句")
            prev = t


if __name__ == "__main__":
    unittest.main(verbosity=2)
