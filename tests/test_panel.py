# -*- coding: utf-8 -*-
"""本机面板：token 鉴权、知识库编辑、记忆只读、搜索调试。

面板是维护者工具，不参与 QQ 连接；测试只验证本地 aiohttp 路由和文件边界。
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, "..")

import panel


class PanelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.knowledge_dir = root / "knowledge"
        self.memory_dir = root / "memory"
        self.knowledge_dir.mkdir()
        self.memory_dir.mkdir()
        (self.knowledge_dir / "all.md").write_text(
            "## 群规\n关键词：群规\n本群不发广告。\n", encoding="utf-8")
        (self.memory_dir / "latest_g1.md").write_text(
            "# 群史记\n\n这是记忆内容。\n", encoding="utf-8")
        (root / "bot.log").write_text("line one\nline two\n", encoding="utf-8")
        (root / "status.json").write_text(json.dumps({
            "updated_at": time.time(),
            "lifecycle": "ready",
            "gateway_connected": True,
            "providers": {
                "providers": [{"label": "测试模型", "models": ["test-model"]}],
                "vision": {"model": "test-model"},
            },
            "budget": {"used": 3, "limit": 100},
        }), encoding="utf-8")
        (root / "state.json").write_text(json.dumps({
            "groups": {"g1": {"last_seen": time.time()}},
            "modes": {"g1": "normal"},
            "digests": {"g1": {"summary": {"updated": time.time()}}},
            "relations": {"g1|u1": {}, "g1|u2": {}},
            "promises": {"g1": []},
        }), encoding="utf-8")
        self.cfg = SimpleNamespace(
            DATA_DIR=str(root),
            KNOWLEDGE_DIR=str(self.knowledge_dir),
            MEMORY_DIR=str(self.memory_dir),
            STATE_FILE=str(root / "state.json"),
            STATUS_FILE=str(root / "status.json"),
            LOG_FILE=str(root / "bot.log"),
            SEARCH_ENABLED=True,
        )
        self.token = "test-token-0123456789abcdef"
        self.app = panel.create_app(self.cfg, token=self.token)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    def auth(self):
        return {panel.TOKEN_HEADER: self.token}

    async def test_requires_token_and_sets_security_headers(self):
        unauth = await self.client.get("/")
        self.assertEqual(unauth.status, 401)
        response = await self.client.get("/", headers=self.auth())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    async def test_query_token_redirects_and_sets_cookie(self):
        response = await self.client.get(f"/?token={self.token}", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertIn("panel_token", response.cookies)
        page = await self.client.get("/")
        self.assertEqual(page.status, 200)

    async def test_dashboard_shows_status_groups_and_knowledge(self):
        response = await self.client.get("/", headers=self.auth())
        text = await response.text()
        self.assertIn("g1", text)
        self.assertIn("3 / 100", text)
        self.assertIn("all.md", text)

    async def test_knowledge_edit_round_trip(self):
        response = await self.client.post(
            "/knowledge/save",
            data={"file": "all.md", "content": "## 新事实\n关键词：测试\n这是新内容。\n"},
            headers=self.auth(), allow_redirects=False)
        self.assertEqual(response.status, 302)
        path = self.knowledge_dir / "all.md"
        self.assertIn("这是新内容", path.read_text(encoding="utf-8"))
        page = await self.client.get("/knowledge?file=all.md", headers=self.auth())
        text = await page.text()
        self.assertIn("这是新内容", text)
        self.assertIn("解析到 1 条", text)

    async def test_new_group_knowledge_file_can_be_created(self):
        response = await self.client.post(
            "/knowledge/save",
            data={"file": "806abc.md", "content": "## 群规\n关键词：群规\n新群规则。\n"},
            headers=self.auth(), allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertTrue((self.knowledge_dir / "806abc.md").is_file())

    async def test_path_traversal_is_rejected(self):
        response = await self.client.post(
            "/knowledge/save",
            data={"file": "../evil.md", "content": "bad"},
            headers=self.auth(), allow_redirects=False)
        self.assertEqual(response.status, 400)
        self.assertFalse((Path(self.cfg.DATA_DIR).parent / "evil.md").exists())

    async def test_memory_is_read_only_and_path_checked(self):
        page = await self.client.get("/memory?file=latest_g1.md", headers=self.auth())
        self.assertEqual(page.status, 200)
        self.assertIn("这是记忆内容", await page.text())
        bad = await self.client.get("/memory?file=../state.json", headers=self.auth())
        self.assertEqual(bad.status, 400)

    async def test_search_debug_uses_search_module(self):
        result = [{"title": "Example", "snippet": "Snippet", "url": "https://example.com/a"}]
        with patch.object(panel.search_mod, "search_web", new=AsyncMock(return_value=result)):
            page = await self.client.get("/search?q=python", headers=self.auth())
        text = await page.text()
        self.assertIn("Example", text)
        self.assertIn("https://example.com/a", text)

    async def test_token_file_is_private_and_stable(self):
        token = panel.load_or_create_token(self.cfg)
        self.assertGreaterEqual(len(token), 24)
        path = Path(self.cfg.DATA_DIR) / panel.TOKEN_FILE
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o777, 0o600)
        self.assertEqual(panel.load_or_create_token(self.cfg), token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
