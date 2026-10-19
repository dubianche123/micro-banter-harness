"""First login errors are actionable; transport noise is not readiness."""
from contextlib import ExitStack
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import bot
import botpy
from botpy.errors import AuthenticationFailedError, ForbiddenError, ServerError
from health import Health


class StartupPolicyTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.client = self.stack.enter_context(patch.object(bot, "GroupBot")).return_value
        self.sleep = self.stack.enter_context(patch.object(bot.time, "sleep"))
        self.stack.enter_context(patch.object(bot.random, "uniform", return_value=1))
        self.stack.enter_context(patch.object(bot.runtime.STATE, "save_now"))
        self.stack.enter_context(patch.object(bot, "_bot_ref", {"client": None}))
        self.health = Health()
        self.stack.enter_context(patch.object(bot, "STATUS", self.health))

    def test_rejected_authentication_stops_without_retry_or_payload(self):
        for error_type in (AuthenticationFailedError, ForbiddenError):
            with self.subTest(error=error_type.__name__):
                self.client.run.side_effect = error_type("private-token-payload")
                self.client.run.reset_mock()
                with self.assertLogs("qqbot", level="ERROR") as captured:
                    self.assertEqual(bot._connect_bot(), 1)
                self.client.run.assert_called_once()
                self.sleep.assert_not_called()
                self.assertIn("QQ_APP_ID / QQ_APP_SECRET", " ".join(captured.output))
                self.assertNotIn("private-token-payload", " ".join(captured.output))
                self.assertEqual(self.health.lifecycle, "error")
                self.assertFalse(self.health.gateway_connected)

    def test_first_connection_failure_is_bounded(self):
        self.client.run.side_effect = TimeoutError("transport timeout")
        with self.assertLogs("qqbot", level="ERROR"):
            self.assertEqual(bot._connect_bot(), 1)
        self.assertEqual(self.client.run.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_transient_initial_failure_can_recover(self):
        self.client.run.side_effect = [TimeoutError(), None]
        self.assertEqual(bot._connect_bot(), 0)
        self.assertEqual(self.client.run.call_count, 2)
        self.sleep.assert_called_once()

    def test_an_established_bot_keeps_reconnecting(self):
        bot._bot_ref["client"] = object()
        self.client.run.side_effect = [TimeoutError(), TimeoutError(), TimeoutError(), None]
        self.assertEqual(bot._connect_bot(), 0)
        self.assertEqual(self.client.run.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_ready_deadline_stops_sdk_internal_retries(self):
        self.client.run.side_effect = bot.QQStartupTimeout()
        with self.assertLogs("qqbot", level="ERROR"):
            self.assertEqual(bot._connect_bot(), 1)
        self.client.run.assert_called_once()
        self.sleep.assert_not_called()

    def test_normal_sdk_stop_does_not_restart(self):
        self.client.run.return_value = None
        self.assertEqual(bot._connect_bot(), 0)
        self.sleep.assert_not_called()

    def test_failed_start_retains_error_snapshot_and_exit_code(self):
        with patch.object(bot, "_run_bot", return_value=1), \
             patch.object(bot.asyncio, "get_event_loop", side_effect=RuntimeError("no loop")):
            self.assertEqual(bot.run_bot(), 1)
        self.assertEqual(self.health.lifecycle, "error")


class LoginBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_rejected_token_becomes_typed_authentication_failure(self):
        client = bot.GroupBot.__new__(bot.GroupBot)
        with patch.object(botpy.Client, "_bot_login", AsyncMock(side_effect=RuntimeError("private-response"))):
            with self.assertRaises(AuthenticationFailedError) as captured:
                await client._bot_login(object())
        self.assertNotIn("private-response", str(captured.exception))

    async def test_sdk_server_error_keeps_its_transport_semantics(self):
        client = bot.GroupBot.__new__(bot.GroupBot)
        original = ServerError("temporary failure")
        with patch.object(botpy.Client, "_bot_login", AsyncMock(side_effect=original)):
            with self.assertRaises(ServerError) as captured:
                await client._bot_login(object())
        self.assertIs(captured.exception, original)

    async def test_native_sdk_requeues_failed_websocket_but_first_ready_still_times_out(self):
        from botpy.connection import ConnectionSession
        from botpy.gateway import BotWebSocket
        client = bot.GroupBot(intents=botpy.Intents(public_messages=True), ext_handlers=False)
        await client._async_setup_hook()
        client.startup_timeout = 0.02
        before = asyncio.all_tasks()

        async def authenticated(self, token):
            self._connection = ConnectionSession(1, self.bot_connect, self.ws_dispatch,
                                                 loop=asyncio.get_running_loop(), api=self.api)
            self._ws_ap = {"shards": 1, "url": "wss://unused.invalid",
                           "session_start_limit": {"remaining": 1, "max_concurrency": 1}}

        try:
            with patch.object(botpy.Client, "_bot_login", authenticated), \
                 patch.object(BotWebSocket, "ws_connect", AsyncMock(side_effect=TimeoutError())) as connect:
                with self.assertRaises(bot.QQStartupTimeout):
                    await client.start(appid="fake", secret="fake")
                connect.assert_awaited_once()
                self.assertEqual(len(client._connection._session_list), 1,
                                 "SDK swallowed the failure and queued another session")
        finally:
            jobs = asyncio.all_tasks() - before
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await client.close()

    async def test_ready_removes_the_initial_deadline(self):
        client = bot.GroupBot.__new__(bot.GroupBot)
        client._ready = asyncio.Event()
        client.startup_timeout = 0.01

        async def connected(self, *args, **kwargs):
            self._ready.set()
            await asyncio.sleep(0.03)
            return "normal stop"

        with patch.object(botpy.Client, "start", connected):
            self.assertEqual(await client.start(appid="fake", secret="fake"), "normal stop")

    async def test_ready_event_confirms_online_status(self):
        health = Health()
        health.lifecycle = "reconnecting"
        client = SimpleNamespace(robot=SimpleNamespace(name="TestBot", id="fake"), _ready=asyncio.Event())
        with patch.object(bot, "STATUS", health), patch.object(bot, "_bot_ref", {}), \
             patch.object(bot.naming, "set_platform_name"), \
             patch.object(bot.config, "CANDIDATE_MODELS", ["fake-model"]), \
             patch.object(bot.config, "DIGEST_ENABLED", False), \
             patch.object(bot.config, "PROMISE_ENABLED", False), \
             patch.object(bot, "probe_provider", AsyncMock()) as probe:
            await bot.GroupBot.on_ready(client)
            self.assertEqual(health.lifecycle, "ready")
            self.assertTrue(health.gateway_connected)
            self.assertTrue(client._ready.is_set())
            self.assertIs(bot._bot_ref["client"], client)
            await asyncio.sleep(0)
            probe.assert_awaited_once()


class GatewayReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_system_traffic_waits_for_authentication_and_resumed_confirms_it(self):
        from botpy.gateway import BotWebSocket
        health = Health()
        health.lifecycle = "reconnecting"
        saved_watch = dict(bot._ws_watch)
        try:
            with patch.object(bot, "STATUS", health), \
                 patch.object(BotWebSocket, "_is_system_event", AsyncMock(return_value=False)):
                bot.install_ws_watchdog()
                gateway = BotWebSocket({}, SimpleNamespace(parser={}))
                for opcode in (10, 11, 9):
                    await gateway._is_system_event({"op": opcode}, "fake-ws")
                    self.assertEqual(health.lifecycle, "reconnecting")
                    self.assertFalse(health.gateway_connected)
                await gateway._is_system_event({"op": 0, "t": "RESUMED"}, "fake-ws")
                self.assertEqual(health.lifecycle, "ready")
                self.assertTrue(health.gateway_connected)
                health.lifecycle = "stopped"
                health.gateway_connected = False
                await gateway._is_system_event({"op": 0, "t": "RESUMED"}, "fake-ws")
                self.assertFalse(health.gateway_connected)
                self.assertEqual(health.lifecycle, "stopped")
        finally:
            bot._ws_watch.clear()
            bot._ws_watch.update(saved_watch)


if __name__ == "__main__":
    unittest.main()
