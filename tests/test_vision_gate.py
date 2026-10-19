"""共同读图服务：不导入 bot、不调用真实模型、用实际原文归档验证关联。"""
import asyncio
from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from archive import MessageArchive
import media
from storage import DailyBudget, StateStore


class CaptionServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = StateStore(str(self.root / "state.json"))
        self.archive = MessageArchive(str(self.root))
        self.buffers = {"g": deque()}
        self.budget = DailyBudget(10, self.state.data["usage"])
        self.calls = []
        self.now = 1792422000.0
        self.options = patch.multiple(media.config, VISION_ENABLED=True,
                                      VISION_PROVIDER="zhipu", VISION_MODEL="vision-test", VISION_INPUT_MODE="url",
                                      VISION_DAILY_CAP=2, VISION_COOLDOWN_SECONDS=60)
        self.options.start()
        async def describe(url):
            self.calls.append(url)
            return "一只猫"
        self.service = self.build(describe=describe)

    async def asyncTearDown(self):
        tasks = list(self.service.inflight.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.options.stop()
        self.archive.close()
        self.tmp.cleanup()

    def build(self, **kwargs):
        return media.CaptionService(self.state, self.archive, self.buffers, self.budget,
                                    clock=lambda: self.now, available=lambda: True, **kwargs)

    def add(self, message_id, text="[图片]", sender="alice", ts=None):
        self.archive.append("g", sender, text, message_id=message_id,
                            ts=self.now if ts is None else ts)
        row = {"message_id": message_id, "sender": sender, "text": text,
               "time": self.now if ts is None else ts}
        self.buffers["g"].append(row)
        return row

    async def caption(self, message_id="m1", url="https://image.invalid/a", **kwargs):
        return await self.service.caption_for("g", message_id, "alice", url, **kwargs)

    def captions(self):
        return [row for row in self.archive.pending("g")[0] if row.get("kind") == "caption"]

    async def test_concurrent_paths_share_request_and_one_vision_slot(self):
        first = self.add("m1", "看看这张 [图片]")
        second = self.add("m2")
        started, release = asyncio.Event(), asyncio.Event()
        async def describe(url):
            self.calls.append(url)
            started.set()
            await release.wait()
            return "一只猫"
        self.service = self.build(describe=describe)
        background = asyncio.create_task(self.caption())
        await started.wait()
        foreground = asyncio.create_task(self.caption("m2", foreground=True))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(background, foreground), ["一只猫", "一只猫"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state.data["vision_usage"]["count"], 1)
        self.assertEqual(first["text"], "看看这张 [图片：一只猫]")
        self.assertEqual(second["text"], "[图片：一只猫]")
        self.assertEqual({row["message_id"] for row in self.captions()}, {"m1", "m2"})

    async def test_cache_hit_consumes_no_new_calls_or_slot(self):
        self.add("m1")
        await self.caption()
        self.add("m2")
        self.budget.limit = 0
        self.assertEqual(await self.caption("m2"), "一只猫")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state.data["vision_usage"]["count"], 1)

    async def test_repeated_message_caption_is_idempotent(self):
        self.add("m1")
        await self.caption()
        await self.caption()
        self.assertEqual(len(self.captions()), 1)
        self.assertEqual(self.buffers["g"][0]["text"], "[图片：一只猫]")

    async def test_cooldown_blocks_background_but_foreground_can_bypass(self):
        self.add("m1")
        await self.caption()
        self.add("m2")
        self.assertIsNone(await self.caption("m2", url="https://image.invalid/b"))
        self.assertEqual(await self.caption("m2", url="https://image.invalid/b", foreground=True), "一只猫")
        self.add("m3")
        self.assertIsNone(await self.caption("m3", url="https://image.invalid/c", foreground=True))
        self.assertEqual(len(self.calls), 2, "前台也不能越过每日图片上限")

    async def test_day_rollover_resets_cap(self):
        self.add("m1")
        self.service.usage.update(day="2000-01-01", count=200)
        self.assertEqual(await self.caption(), "一只猫")
        self.assertEqual(self.service.usage["count"], 1)

    async def test_disabled_vision_never_calls_model(self):
        self.add("m1")
        with patch.object(media.config, "VISION_ENABLED", False):
            self.assertIsNone(await self.caption())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.usage, {})

    async def test_exhausted_global_budget_does_not_claim_vision_slot(self):
        self.add("m1")
        self.budget.limit = 0
        self.assertIsNone(await self.caption(foreground=True))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.usage, {})

    async def test_missing_budget_does_not_claim_slot(self):
        self.add("m1")
        self.service.budget = None
        self.assertIsNone(await self.caption())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.usage, {})

    async def test_missing_credentials_does_not_claim_slot(self):
        self.add("m1")
        self.service.available = lambda: False
        self.assertIsNone(await self.caption())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.usage, {})

    async def test_message_id_and_sender_prevent_wrong_image_replacement(self):
        neighbor = self.add("other", sender="bob")
        target = self.add("m1", "这张图 [图片]")
        await self.caption()
        self.assertEqual(neighbor["text"], "[图片]")
        self.assertEqual(target["text"], "这张图 [图片：一只猫]")
        self.assertEqual(self.state.data["buffers"]["g"][1]["text"], target["text"])
        self.assertEqual(self.captions()[0]["message_id"], "m1")

    async def test_same_source_id_with_wrong_sender_is_not_attached(self):
        target = self.add("m1", sender="bob")
        await self.caption()
        self.assertEqual(target["text"], "[图片]")
        self.assertEqual(self.captions(), [])

    async def test_description_failure_leaves_original_text(self):
        row = self.add("m1", "看看 [图片]")
        async def fail(url):
            return None
        self.service = self.build(describe=fail)
        self.assertIsNone(await self.caption())
        self.assertEqual(row["text"], "看看 [图片]")
        self.assertEqual(self.service.cache, {})

    async def test_restart_reuses_saved_captions_and_vision_usage(self):
        self.add("m1")
        await self.caption()
        self.state.save_now()
        new_state = StateStore(str(self.root / "state.json"))
        async def must_not_call(url):
            self.fail("重启后已有描述不得重新调用模型")
        self.service = media.CaptionService(new_state, self.archive, self.buffers, self.budget,
                                            describe=must_not_call, available=lambda: False)
        self.add("m2")
        self.assertEqual(await self.caption("m2"), "一只猫")
        self.assertEqual(new_state.data["vision_usage"]["count"], 1)

    async def test_cache_bound_evicts_only_oldest_entry(self):
        self.service = self.build(cache_size=2, describe=self.service.describe)
        for index in range(3):
            self.add(f"m{index}")
            self.now += 61
            with patch.object(media.config, "VISION_DAILY_CAP", 10):
                await self.caption(f"m{index}", url=f"url{index}")
        self.assertEqual(list(self.service.cache), ["url1", "url2"])

    async def test_cancelled_waiter_does_not_cancel_shared_description(self):
        self.add("m1")
        self.add("m2")
        started, release = asyncio.Event(), asyncio.Event()
        async def describe(url):
            self.calls.append(url)
            started.set()
            await release.wait()
            return "一只猫"
        self.service = self.build(describe=describe)
        cancelled = asyncio.create_task(self.caption())
        await started.wait()
        survivor = asyncio.create_task(self.caption("m2", foreground=True))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        release.set()
        self.assertEqual(await survivor, "一只猫")
        self.assertEqual(len(self.calls), 1)

    async def test_foreground_upgrades_background_before_its_cooldown_check(self):
        self.add("m1")
        self.service.usage.update(day=media.time.strftime("%Y-%m-%d", media.time.localtime(self.now)),
                                  count=0, last=self.now)
        background = asyncio.create_task(self.caption())
        foreground = asyncio.create_task(self.caption(foreground=True))
        self.assertEqual(await asyncio.gather(background, foreground), ["一只猫", "一只猫"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.service.usage["count"], 1)

    async def test_provider_budget_competition_does_not_overclaim_vision_slots(self):
        self.add("m1")
        self.add("m2")
        self.budget.limit = 1
        async def create(**kwargs):
            self.calls.append(kwargs)
            await asyncio.sleep(0)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="一只猫"))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.service = self.build()
        with patch.dict(media.providers.AI_CLIENTS, {"zhipu": [client]}, clear=True), \
             patch.object(media.providers, "_budget", self.budget), \
             patch.object(media.providers, "_on_usage", self.state.mark_dirty), \
             patch.object(media.providers, "_initialized", True):
            results = await asyncio.gather(self.caption(), self.caption("m2", url="other", foreground=True))
        self.assertEqual(results, ["一只猫", None])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.budget.used, 1)
        self.assertEqual(self.service.usage["count"], 1)

    async def test_cancelled_only_waiter_still_links_completed_evidence(self):
        target = self.add("m1")
        started, release = asyncio.Event(), asyncio.Event()
        async def describe(url):
            self.calls.append(url)
            started.set()
            await release.wait()
            return "一只猫"
        self.service = self.build(describe=describe)
        waiter = asyncio.create_task(self.caption())
        await started.wait()
        production = next(iter(self.service.inflight.values()))
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        self.assertEqual(await production, "一只猫")
        self.assertEqual(target["text"], "[图片：一只猫]")
        self.assertEqual([row["message_id"] for row in self.captions()], ["m1"])

    async def test_foreground_with_one_remaining_request_keeps_reply_opportunity(self):
        self.add("m1")
        self.budget.limit = 1
        self.assertIsNone(await self.caption(foreground=True))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.usage, {})
        self.assertTrue(self.budget.try_consume(), "正文仍有一次真实请求机会")

    async def test_close_waits_for_shielded_producer_and_saves_evidence(self):
        self.add("m1")
        started, release = asyncio.Event(), asyncio.Event()
        async def describe(url):
            started.set()
            await release.wait()
            return "一只猫"
        self.service = self.build(describe=describe)
        waiter = asyncio.create_task(self.caption())
        await started.wait()
        shutdown = asyncio.create_task(self.service.close())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await shutdown
        self.assertEqual(await waiter, "一只猫")
        self.assertEqual(len(self.captions()), 1)
        self.assertIsNone(await self.caption())

    async def test_close_cancel_stops_the_actual_shielded_producer(self):
        self.add("m1")
        started = asyncio.Event()
        async def describe(url):
            started.set()
            await asyncio.Event().wait()
        self.service = self.build(describe=describe)
        waiter = asyncio.create_task(self.caption())
        await started.wait()
        await self.service.close(cancel=True)
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(self.service.inflight, {})
        self.assertEqual(self.service.cache, {})
