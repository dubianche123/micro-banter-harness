# -*- coding: utf-8 -*-
"""可编辑知识库：维护者改文件，关键词命中才进动态层。

守护三件事：群文件只对本群生效、同名条目由群文件覆盖、改完文件不用重启。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, "..")

import bot
import config
import knowledge


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.write("all.md", """# 共享
## 群规
关键词：群规、广告
本群不发广告。

## 群主
关键词：群主
共享版本的群主事实。
""")
        self.write("g1.md", """# 一群
## 奶茶
关键词：奶茶、请客
阿强答应请全群喝奶茶。

## 群主
关键词：群主
一群自己的群主事实。
""")
        self.kb = knowledge.KnowledgeBase(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as stream:
            stream.write(text)

    def test_group_and_shared_files_match_their_scope(self):
        self.assertIn("阿强答应请全群喝奶茶", self.kb.render("g1", "奶茶呢"))
        self.assertIn("本群不发广告", self.kb.render("g1", "群规是什么"))
        self.assertIn("本群不发广告", self.kb.render("g2", "群规是什么"))
        self.assertEqual(self.kb.render("g2", "奶茶呢"), "")

    def test_group_entry_overrides_shared_title(self):
        self.assertIn("一群自己的群主事实", self.kb.render("g1", "群主是谁"))
        self.assertNotIn("共享版本", self.kb.render("g1", "群主是谁"))
        self.assertIn("共享版本", self.kb.render("g2", "群主是谁"))

    def test_title_is_keyword_without_keyword_line(self):
        self.write("g1.md", "## 暗号\n进群先喊芝麻开门。\n")
        self.kb = knowledge.KnowledgeBase(self.dir)
        self.assertIn("芝麻开门", self.kb.render("g1", "暗号是啥"))

    def test_sensitive_entry_is_dropped(self):
        self.write("g1.md", "## 测试\n关键词：测试\n主席相关的内容。\n")
        self.kb = knowledge.KnowledgeBase(self.dir)
        self.assertEqual(self.kb.render("g1", "测试一下"), "")

    def test_machine_id_is_scrubbed(self):
        self.write("g1.md", "## 编号\n关键词：编号\n联系 0123456789ABCDEF0123456789ABCDEF\n")
        self.kb = knowledge.KnowledgeBase(self.dir)
        out = self.kb.render("g1", "编号在哪")
        self.assertIn("群友编号", out)
        self.assertNotIn("0123456789ABCDEF", out)

    def test_caps_items(self):
        rows = "\n".join(
            f"## 条目{i}\n关键词：测试\n" + ("内容" * 10) for i in range(5))
        self.write("g1.md", rows)
        self.kb = knowledge.KnowledgeBase(self.dir)
        with patch.multiple(config, KNOWLEDGE_MAX_ITEMS=2, KNOWLEDGE_MAX_CHARS=1000):
            out = self.kb.render("g1", "测试")
        self.assertEqual(out.count("\n- "), 2)

    def test_caps_chars(self):
        self.write("g1.md", "## 长条目\n关键词：测试\n" + ("内容" * 100))
        self.kb = knowledge.KnowledgeBase(self.dir)
        with patch.multiple(config, KNOWLEDGE_MAX_ITEMS=2, KNOWLEDGE_MAX_CHARS=120):
            out = self.kb.render("g1", "测试")
        self.assertLessEqual(len(out), len(knowledge.HEADER) + 120 + 2)

    def test_edit_takes_effect_without_restart(self):
        self.assertEqual(self.kb.render("g1", "新事实呢"), "")
        self.write("g1.md", "## 新事实\n关键词：新事实\n今天刚加的事实。\n")
        time.sleep(0.01)
        self.assertIn("今天刚加的事实", self.kb.render("g1", "新事实呢"))

    def test_header_says_maintainer_facts_win(self):
        out = self.kb.render("g1", "群规")
        self.assertIn(knowledge.KNOWLEDGE_MARK, out)
        self.assertIn("以此为准", out)
        self.assertIn("不要复述整条", out)

    def test_disabled_by_config(self):
        with patch.object(config, "KNOWLEDGE_ENABLED", False):
            self.assertEqual(self.kb.render("g1", "群规"), "")

    def test_bot_wires_knowledge_into_turn(self):
        import inspect
        src = inspect.getsource(bot)
        self.assertIn("runtime.KNOWLEDGE.render", src)
        self.assertIn("knowledge_note=knowledge_note", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
