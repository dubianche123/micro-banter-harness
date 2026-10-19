"""Background status uses real task transitions, independent of logs."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from health import Health


class HealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_task_is_visible_in_atomic_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            health = Health()
            path = Path(directory) / "status.json"
            health.configure(path, lambda: {"budget": {"remaining": 3}})
            async def fail():
                raise RuntimeError("test failure")
            await health.run("digest", fail)
            data = json.loads(path.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["tasks"]["digest"]["status"], "failed")
            self.assertEqual(data["tasks"]["digest"]["last_error"], "RuntimeError")
            self.assertEqual(data["budget"]["remaining"], 3)

    async def test_waiting_and_cancellation_are_real_task_states(self):
        health = Health()
        async def wait():
            await health.sleep(100)
        task = asyncio.create_task(health.run("flush", wait))
        await asyncio.sleep(0)
        self.assertEqual(health.tasks["flush"]["status"], "waiting")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(health.tasks["flush"]["status"], "cancelled")

    async def test_dedicated_runtime_shutdown_cancels_untracked_sdk_jobs(self):
        from unittest.mock import AsyncMock, patch
        import bot
        started = asyncio.Event()
        async def sdk_job():
            started.set()
            await asyncio.Event().wait()
        job = asyncio.create_task(sdk_job())
        await started.wait()
        with patch.object(bot.media, "close", AsyncMock()) as media_close, \
             patch.object(bot.providers, "close", AsyncMock()) as provider_close:
            await bot._shutdown(cancel_all=True)
        self.assertTrue(job.cancelled())
        media_close.assert_awaited_once_with(cancel=True)
        provider_close.assert_awaited_once()
