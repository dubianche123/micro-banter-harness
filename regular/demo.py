"""Offline console transport for the production QQ message handler."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid


class DemoSession:
    """A fresh CLI process gets temporary state and a fixed, explicitly fake model."""

    async def __aenter__(self):
        if any(name in sys.modules for name in ("bot", "runtime", "config")):
            raise ValueError("Start the offline demo in a fresh process: regular demo")
        self.stack = ExitStack()
        self.home = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="regular-demo-"))
        home = Path(self.home)
        self.original_directory = Path.cwd()
        self.stack.callback(os.chdir, self.original_directory)
        os.chdir(home)  # the upstream QQ SDK's own log also stays inside the temporary demo
        environment = {"REGULAR_HOME": str(home), "DATA_DIR": str(home / "data"),
            "QQ_APP_ID": "", "QQ_APP_SECRET": "", "ZHIPU_API_KEY": "",
            "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "AI_PROVIDER": "zhipu", "FALLBACK_PROVIDER": "",
            "BOT_NAMES": "老群友", "VISION_ENABLED": "false"}
        self.stack.enter_context(patch.dict(os.environ, environment))
        try:
            import bot
            self.bot = bot
            async def fake_model(messages, max_tokens, tier="chat", **kwargs):
                if tier == "judge":
                    return "OK"
                if tier == "digest":
                    return '离线演示：群友在试用称呼、关系和本地指令。<DIGEST>' + json.dumps({
                        "topics": ["离线体验"], "people": [], "memes": [],
                        "promises": [], "affinity": []}, ensure_ascii=False) + '</DIGEST>'
                return '（离线演示）在呢。称呼、骰子、模式和查档走的都是真正的本地处理器。<CMD>{"aff":0}</CMD>'
            self.stack.enter_context(patch.object(bot, "call_model", fake_model))
            self.client = SimpleNamespace(robot=SimpleNamespace(id="demo-bot", name="老群友"))
            return self
        except BaseException:
            self.stack.close()
            raise

    async def reply(self, text, *, user_id="alice", group_id="demo-group", addressed=True):
        replies = []
        async def sent(**payload):
            replies.append(payload["content"])
            return {"id": "demo-receipt-" + str(len(replies))}
        message = SimpleNamespace(id=uuid.uuid4().hex, content=text, group_openid=group_id,
            author=SimpleNamespace(member_openid=user_id), attachments=[], mentions=[],
            timestamp=datetime.now(timezone.utc).isoformat(), reply=sent)
        await self.bot.GroupBot.handle_group_msg(self.client, message, is_at=addressed)
        return "\n".join(replies) if replies else "（本轮保持安静）"

    async def __aexit__(self, *exc):
        try:
            await self.bot._shutdown()
            self.bot.runtime.ARCHIVE.close()
        finally:
            self.stack.close()
