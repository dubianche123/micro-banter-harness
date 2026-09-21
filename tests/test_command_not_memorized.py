# -*- coding: utf-8 -*-
"""「本地控制指令不许进长期记忆」的回归测试。

事故（2026-09-21 群主反馈）：群主喊了一句「猫娘模式」切换人设，结果每日压缩把
它当成了人物事实 —— 摘要里留下「老王还想当猫娘」，还被安到别人头上。
根因两条：
1. 归档发生在所有本地指令分支**之前**，指令跟普通聊天一样进了归档；
2. 压缩把归档当聊天内容读，操作被理解成这个人的想法。
修法：归档时给指令打 cmd 标记，压缩取数（archive.since）默认跳过；压缩提示词
再加一条规则兜住没标记的存量。
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "..")

import archive as archive_mod
import bot
import digest as digest_mod


class IsControlCommandTest(unittest.TestCase):
    def test_mode_switch_is_a_command(self):
        # 用真实的触发词字面：退出那档是「退出猫娘/恢复正常」这类，没有「退出模式」
        for text in ("猫娘模式", "切换到发疯模式", "变身猫娘", "退出猫娘", "恢复正常"):
            self.assertTrue(bot.is_control_command(text), text)

    def test_local_queries_are_commands(self):
        for text in ("查好感", "关系榜", "称呼表", "群史记", "决斗", "办他"):
            self.assertTrue(bot.is_control_command(text), text)

    def test_ordinary_chat_is_not_a_command(self):
        for text in ("今天天气不错", "小王你疯了", "我想把⭐🧊传染给群友", "", "   "):
            self.assertFalse(bot.is_control_command(text), text)

    def test_rename_lock_is_a_command(self):
        self.assertTrue(bot.is_control_command("锁起名"))


class CommandNeverReachesCompressionTest(unittest.TestCase):
    """打了 cmd 标记的消息：磁盘上留痕，但压缩取数时看不见。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ar = archive_mod.MessageArchive(self.tmp, keep_days=7)

    def tearDown(self):
        self.ar.close()

    def _archive(self, gid, sender, text, **kw):
        row = self.ar.append(gid, sender, text, **kw)
        self.ar.close()
        return row

    def test_cmd_row_is_marked_on_disk(self):
        row = self._archive("g1", "u1", "猫娘模式", cmd=True)
        self.assertEqual(row.get("cmd"), 1, "指令要打标记，否则压缩读不出来它是指令")

    def test_since_skips_commands(self):
        self._archive("g1", "u1", "猫娘模式", cmd=True)
        self._archive("g1", "u1", "今天天气不错")
        rows = self.ar.since("g1", 0)
        texts = [r["text"] for r in rows]
        self.assertNotIn("猫娘模式", texts, "指令被压缩读走了，会变成人物事实")
        self.assertIn("今天天气不错", texts)

    def test_include_cmd_can_still_read_them(self):
        """留个后门：真要审计时还是能读出来（落盘留痕的意义）。"""
        self._archive("g1", "u1", "猫娘模式", cmd=True)
        self.assertEqual(len(self.ar.since("g1", 0, include_cmd=True)), 1)


class DigestPromptKnowsCommandsTest(unittest.TestCase):
    def test_reduce_prompt_told_not_to_record_commands(self):
        """存量归档里还有没标记的旧指令，靠提示词这层兜住。"""
        self.assertIn("不是聊天内容", digest_mod.SYSTEM_REDUCE)
        self.assertIn("猫娘模式", digest_mod.SYSTEM_REDUCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
