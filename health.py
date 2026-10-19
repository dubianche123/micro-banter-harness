"""Small structured runtime snapshot; task health comes from actual loop ticks."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import os
import time

from storage import atomic_write_json

logger = logging.getLogger("qqbot")
_active = ContextVar("regular_health_task", default=None)


class Health:
    def __init__(self):
        self.lifecycle = "starting"
        self.gateway_connected = False
        self.tasks = {}
        self.path = None
        self.details = lambda: {}

    def configure(self, path, details):
        self.path, self.details = path, details

    def write(self):
        if not self.path:
            return
        try:
            atomic_write_json(self.path, {"schema_version": 1, "updated_at": time.time(),
                "pid": os.getpid(), "lifecycle": self.lifecycle,
                "gateway_connected": self.gateway_connected, "tasks": self.tasks,
                **self.details()})
        except OSError as error:
            logger.warning("运行状态快照无法写入: %s", error)

    async def sleep(self, seconds):
        name = _active.get()
        if name:
            slot = self.tasks[name]
            slot.update(status="waiting", last_tick=time.time(), next_tick=time.time() + seconds)
            self.write()
        await asyncio.sleep(seconds)
        if name:
            self.tasks[name].update(status="running", last_tick=time.time())
            self.write()

    def error(self, error):
        name = _active.get()
        if name:
            self.tasks[name].update(status="error", last_error=type(error).__name__, last_tick=time.time())
            self.write()

    async def run(self, name, factory):
        self.tasks[name] = {"status": "starting", "last_tick": time.time()}
        token = _active.set(name)
        try:
            await factory()
            self.tasks[name]["status"] = "stopped"
        except asyncio.CancelledError:
            self.tasks[name]["status"] = "cancelled"
            raise
        except Exception as error:
            self.tasks[name].update(status="failed", last_error=type(error).__name__)
            logger.exception("后台任务已退出: %s", name)
        finally:
            _active.reset(token)
            self.write()


STATUS = Health()
