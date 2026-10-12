# -*- coding: utf-8 -*-
"""断网补投队列的回归测试（2026-10-12 实测事故）。

下午 16:42 本机断网约 5 分钟：两家 LLM 同时超时，兜底回复在重连前 47 秒
发出，ConnectionError 被记 ❌ 后就没了 —— 而重连发生在被动时效内，本来
来得及补。修复后：连接类发送失败入队，resend_queue_loop 每 10 秒补投
一轮，过了被动时效余量才放弃。
"""
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "..")

from botpy.message import GroupMessage  # noqa: E402

import bot  # noqa: E402

GROUP = "TESTGROUP_RESEND"
USER = "TESTUSER_RESEND"


def _stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).astimezone().isoformat()


class FakeGroupMessage:
    """reply 可编程的假群消息：脚本化每次调用的行为。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.id = "MSG_RESEND"
        self.group_openid = GROUP
        self.timestamp = _stamp(1)

    async def reply(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.script.pop(0) if self.script else {"id": "ok"}
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def _conn_error():
    return RuntimeError("Cannot connect to host bots.qq.com:443 ssl:default [None]")


class ResendQueueTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        bot._FAILED_SENDS.clear()

    def tearDown(self):
        bot._FAILED_SENDS.clear()

    async def test_connection_error_enqueues_and_drain_delivers(self):
        """发送撞连接错误 → 入队；网络回来后补投成功、队列清空。"""
        m = FakeGroupMessage([_conn_error(), {"id": "late-1"}])
        await bot.safe_reply(m, "补投测试")
        self.assertEqual(len(bot._FAILED_SENDS), 1, "连接失败应当入队")
        self.assertEqual(m.calls[0].get("content"), "补投测试")

        await bot._drain_once()
        self.assertEqual(len(m.calls), 2, "网络恢复后应当补投一次")
        self.assertEqual(len(bot._FAILED_SENDS), 0, "补投成功后队列清空")

    async def test_drain_keeps_everything_while_network_is_down(self):
        """网络还没回来：原样留在队里，且保持先来后到的顺序。"""
        m1 = FakeGroupMessage([_conn_error()])
        m2 = FakeGroupMessage([_conn_error()])
        now = time.time()
        bot._FAILED_SENDS.append((m1, "第一条", now))
        bot._FAILED_SENDS.append((m2, "第二条", now + 1))

        await bot._drain_once()
        self.assertEqual([r for _, r, _ in bot._FAILED_SENDS], ["第一条", "第二条"])
        self.assertEqual(len(m1.calls), 1)
        self.assertEqual(len(m2.calls), 0, "第一发就撞墙，后面别去撞")

    async def test_entries_past_the_window_are_dropped(self):
        """滞留到被动时效余量之外：放弃补投（腾讯必拒 40034005），队列清空。"""
        m = FakeGroupMessage([])
        stale = 1000.0 - (bot.GROUP_PASSIVE_WINDOW_SECONDS + 60)
        bot._FAILED_SENDS.append((m, "过期回复", stale))
        await bot._drain_once()
        self.assertEqual(m.calls, [], "过期的补投不该真发出去")
        self.assertEqual(len(bot._FAILED_SENDS), 0)

    async def test_seq_conflict_on_drain_drops_the_entry(self):
        """补投被 msg_seq 去重拒绝 = 首条其实已送达，从队列里撤下即可。"""
        m = FakeGroupMessage([RuntimeError("msg_seq 重复")])
        bot._FAILED_SENDS.append((m, "已送达的", 1000.0))
        await bot._drain_once()
        self.assertEqual(len(bot._FAILED_SENDS), 0, "去重拒绝不算失败，撤下不再试")

    async def test_double_timeout_also_queues(self):
        """botpy 吞超时返回 None 且立即重试仍是 None：同样要入队（10-08 修的
        None 检测只重试了一次，重试也失败时原来只会记 ❌ 然后丢掉）。"""
        m = FakeGroupMessage([None, None])
        await bot.safe_reply(m, "两次都没出去")
        self.assertEqual(len(bot._FAILED_SENDS), 1)

    async def test_queue_is_bounded(self):
        """队列有上限：断网期间狂刷不会把内存吃穿。"""
        m = FakeGroupMessage([_conn_error()] * 25)
        for _ in range(25):
            await bot.safe_reply(m, "刷屏")
        self.assertLessEqual(len(bot._FAILED_SENDS), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
