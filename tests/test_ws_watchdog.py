# -*- coding: utf-8 -*-
"""网关看门狗回归测试。

守护的场景（2026-09-19 13:15 实测）：一次 1006 之后 botpy 整整 18 分钟没有任何重连动作
—— 接收循环挂在半死的 TCP 上，不报错、不重连、连日志都没有，群里看到的就是「它死了」。
看门狗的约定：网关静默（连心跳 ACK 都没来）超过阈值就主动断开 ws，把 botpy 自己的
重连链路踹醒。
"""
import asyncio
import sys
import time
import unittest
import types

sys.path.insert(0, "..")

import config


class WsWatchdogTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import bot  # noqa: E402
        cls.bot = bot

    def setUp(self):
        self.saved = dict(self.bot._ws_watch)
        self.bot._ws_watch.clear()
        self.bot._ws_watch.update(last_rx=0.0, conn=None, kicks=0)

    def tearDown(self):
        self.bot._ws_watch.clear()
        self.bot._ws_watch.update(self.saved)

    def _fake_conn(self, closed=False):
        kicked = {"code": None, "n": 0}

        async def close(code=1000):
            kicked["code"] = code
            kicked["n"] += 1

        conn = types.SimpleNamespace(closed=closed, close=close)
        return conn, kicked

    def test_hook_records_every_downstream_message(self):
        """_is_system_event 被包过之后，每一条下行（含心跳 ACK）都要留下时刻。"""
        from botpy.gateway import BotWebSocket

        self.bot.install_ws_watchdog()
        ws_obj = BotWebSocket({}, types.SimpleNamespace(parser={}))
        # op=11 是 HEARTBEAT_ACK：最不起眼的下行，恰恰是看门狗唯一的信号源
        got = asyncio.run(ws_obj._is_system_event({"op": 11}, "FAKE_WS"))
        self.assertTrue(got, "心跳 ACK 仍应按系统事件吞掉，不能改变原行为")
        self.assertEqual(self.bot._ws_watch["conn"], "FAKE_WS",
                         "钩子必须把 ws 对象存下来，看门狗踢人全靠它")
        self.assertGreater(self.bot._ws_watch["last_rx"], 0.0, "时刻必须被刷新")

    async def test_quiet_gateway_within_threshold_is_left_alone(self):
        """心跳正常（90s 内有动静）时绝不踢。"""
        conn, kicked = self._fake_conn()
        self.bot._ws_watch.update(last_rx=time.time() - 30, conn=conn)
        self.assertFalse(await self.bot._ws_watchdog_tick())
        self.assertEqual(kicked["n"], 0)

    async def test_stale_gateway_gets_kicked(self):
        """核心诉求：静默超阈值 → 主动断开 ws，把重连链路踹醒。"""
        conn, kicked = self._fake_conn()
        self.bot._ws_watch.update(last_rx=time.time() - 18 * 60, conn=conn)  # 复刻 13:15 事故
        self.assertTrue(await self.bot._ws_watchdog_tick())
        self.assertEqual(kicked["n"], 1, "必须真的调 close，botpy 的重连链路靠这个醒")
        self.assertEqual(kicked["code"], 4000)
        # 计时被重置：防止重连进行中被连环误踢
        self.assertLess(time.time() - self.bot._ws_watch["last_rx"], 1.0)
        self.assertEqual(self.bot._ws_watch["kicks"], 1)

    async def test_kick_is_not_repeated_while_reconnecting(self):
        """踢完立刻重置计时 → 刚踢过的 15s 后那一拍不会再踢。"""
        conn, kicked = self._fake_conn()
        self.bot._ws_watch.update(last_rx=time.time() - 18 * 60, conn=conn)
        await self.bot._ws_watchdog_tick()
        again = await self.bot._ws_watchdog_tick()
        self.assertFalse(again, "刚踢过就重置了计时，下一拍不该再踢")
        self.assertEqual(kicked["n"], 1)

    async def test_stale_with_closed_conn_does_not_double_kick(self):
        """连接已经是死的：重连链路理应在跑，看门狗不补刀、也不刷屏。"""
        conn, kicked = self._fake_conn(closed=True)
        self.bot._ws_watch.update(last_rx=time.time() - 18 * 60, conn=conn)
        self.assertFalse(await self.bot._ws_watchdog_tick())
        self.assertEqual(kicked["n"], 0)
        self.assertLess(time.time() - self.bot._ws_watch["last_rx"], 1.0)

    async def test_never_seen_anything_yet_is_safe(self):
        """首连之前 last_rx=0：什么都不做，而不是把 epoch 当成静默一万年。"""
        conn, kicked = self._fake_conn()
        self.bot._ws_watch.update(last_rx=0.0, conn=conn)
        self.assertFalse(await self.bot._ws_watchdog_tick())
        self.assertEqual(kicked["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
