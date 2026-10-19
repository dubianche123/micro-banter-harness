"""Delivery and history invariants with fake QQ receipts and model output."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
import bot
from delivery import Sender
from storage import SessionStore


def message(script=None, identity="source"):
    return SimpleNamespace(id=identity, group_openid="commit-group",
        timestamp=datetime.now(timezone.utc).isoformat(),
        reply=AsyncMock(side_effect=script or [{"id": "receipt"}]))


class DeliveryCommitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions = SessionStore()
        self.state = SimpleNamespace(data={"groups": {}}, mark_dirty=lambda: None)
        self.sender = Sender(fallback=lambda _: "platform fallback")
        for target, name, value in ((bot.config, "OWNER_CLAIM_PHRASE", "test-only-private-phrase"),
                                    (bot.runtime, "SESSIONS", self.sessions),
                                    (bot.runtime, "STATE", self.state),
                                    (bot, "SENDER", self.sender)):
            context = patch.object(target, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(bot, "get_ai_reply", AsyncMock(return_value=("candidate", 1)))
        self.model = context.start()
        self.addCleanup(context.stop)

    def history(self):
        return self.sessions._sessions.get("g_u", {}).get("messages", [])

    async def test_positive_receipt_commits_actual_fallback_only(self):
        done = []
        m = message([RuntimeError("40034006"), {"id": "fallback"}])
        _, _, result = await bot.reply_in_session(m, "g_u", "first", on_delivered=lambda: done.append(1))
        self.assertTrue(result.delivered)
        self.assertEqual(self.history()[-1]["content"], "platform fallback")
        self.assertEqual(done, [])
        self.assertNotEqual(m.reply.call_args_list[0].kwargs["msg_seq"],
                            m.reply.call_args_list[1].kwargs["msg_seq"])

    async def test_rejected_reply_never_enters_history(self):
        _, _, result = await bot.reply_in_session(message([RuntimeError("denied")]), "g_u", "first")
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.history(), [])

    async def test_queued_reply_commits_once_after_receipt(self):
        done = []
        m = message([None, None, {"id": "late"}])
        await bot.reply_in_session(m, "g_u", "first", on_delivered=lambda: done.append(1))
        self.assertEqual(self.history(), [])
        await self.sender.drain()
        await self.sender.drain()
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(done, [1])
        self.assertEqual({call.kwargs["msg_seq"] for call in m.reply.call_args_list}, {1})

    async def test_prune_keeps_recent_queue_without_recording_candidate(self):
        m = message([None, None, {"id": "late"}])
        await bot.reply_in_session(m, "g_u", "first")
        self.sessions.prune()
        self.assertEqual(self.history(), [])
        await self.sender.drain()
        self.assertEqual(len(self.history()), 2)

    async def test_expired_queue_never_commits(self):
        await bot.reply_in_session(message([None, None]), "g_u", "first")
        self.sender.pending[0].created_at -= 1000
        await self.sender.drain()
        self.assertEqual(self.history(), [])

    async def test_new_turn_supersedes_unconfirmed_old_turn(self):
        old = message([None, None, {"id": "old"}], "old")
        await bot.reply_in_session(old, "g_u", "first")
        await bot.reply_in_session(message(identity="new"), "g_u", "second")
        await self.sender.drain()
        self.assertEqual(old.reply.await_count, 2)
        self.assertEqual([m["content"] for m in self.history()], ["second", "candidate"])

    async def test_new_failed_generation_also_supersedes_old_queue(self):
        old = message([None, None, {"id": "old"}], "old")
        await bot.reply_in_session(old, "g_u", "first")
        self.model.return_value = (None, None)
        await bot.reply_in_session(message(identity="new"), "g_u", "second")
        await self.sender.drain()
        self.assertEqual(old.reply.await_count, 2)
        self.assertEqual(len(self.history()), 2)

    async def test_reset_during_generation_prevents_stale_send(self):
        ready, resume = asyncio.Event(), asyncio.Event()
        async def generate(*args, **kwargs):
            ready.set()
            await resume.wait()
            return "old mode", None
        self.model.side_effect = generate
        m = message()
        task = asyncio.create_task(bot.reply_in_session(m, "g_u", "first"))
        await ready.wait()
        self.sessions.clear_group("g")
        resume.set()
        _, _, result = await task
        self.assertEqual(result.status, "superseded")
        m.reply.assert_not_awaited()

    async def test_reset_between_immediate_attempts_prevents_new_retry(self):
        sending, resume = asyncio.Event(), asyncio.Event()
        async def unconfirmed(**kwargs):
            sending.set()
            await resume.wait()
            return None
        m = message()
        m.reply.side_effect = unconfirmed
        task = asyncio.create_task(bot.reply_in_session(m, "g_u", "first"))
        await sending.wait()
        self.sessions.clear_group("g")
        resume.set()
        _, _, result = await task
        self.assertEqual(result.status, "superseded")
        self.assertEqual(m.reply.await_count, 1)
        self.assertEqual(self.history(), [])

    async def test_reset_also_removes_queued_old_candidate(self):
        m = message([None, None, {"id": "old"}])
        await bot.reply_in_session(m, "g_u", "first")
        self.sessions.drop_session("g_u")
        await self.sender.drain()
        self.assertEqual(m.reply.await_count, 2)
        self.assertEqual(self.history(), [])

    async def test_milestone_and_owner_hint_wait_for_real_delivery(self):
        done = []
        m = message([None, None, {"id": "late"}])
        await bot.reply_in_session(m, "g_u", "first", owner_hint_group="g",
                                   on_delivered=lambda: done.append(1))
        self.assertFalse(self.state.data["groups"].get("g", {}).get("owner_hint"))
        self.assertEqual(done, [])
        await self.sender.drain()
        self.assertTrue(self.state.data["groups"]["g"]["owner_hint"])
        self.assertEqual(done, [1])

    async def test_no_model_fallback_keeps_one_shot_pending(self):
        self.model.return_value = (None, None)
        done = []
        await bot.reply_in_session(message(), "g_u", "first", owner_hint_group="g",
                                   on_delivered=lambda: done.append(1))
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(done, [])
        self.assertNotIn("g", self.state.data["groups"])

    async def test_retry_and_new_turn_share_the_same_lock(self):
        old = message([None, None], "old")
        await bot.reply_in_session(old, "g_u", "first")
        sending, resume = asyncio.Event(), asyncio.Event()
        async def delayed_send(**kwargs):
            sending.set()
            await resume.wait()
            return {"id": "old"}
        old.reply.side_effect = delayed_send
        retry = asyncio.create_task(self.sender.drain())
        await sending.wait()
        self.model.reset_mock()
        next_turn = asyncio.create_task(bot.reply_in_session(message(identity="new"), "g_u", "second"))
        await asyncio.sleep(0)
        self.model.assert_not_awaited()
        resume.set()
        await retry
        await next_turn
        self.assertEqual([m["content"] for m in self.history()][::2], ["first", "second"])

    async def test_actual_aiohttp_disconnects_queue(self):
        for error in (aiohttp.ServerDisconnectedError("Server disconnected"),
                      aiohttp.ClientOSError(54, "Connection reset by peer")):
            result = await self.sender.reply(message([error]), "retry me")
            self.assertEqual(result.status, "queued")

    async def test_prune_retains_active_session_lock_and_waiters(self):
        self.sessions.record("g_u", "first", "reply", now=1)
        lock = self.sessions.lock("g_u")
        async with lock:
            waiter = asyncio.create_task(bot.reply_in_session(message(), "g_u", "next"))
            await asyncio.sleep(0)
            self.sessions.prune(now=100000)
            self.assertIs(self.sessions.lock("g_u"), lock)
            self.assertEqual(lock.users, 2)
        await waiter
