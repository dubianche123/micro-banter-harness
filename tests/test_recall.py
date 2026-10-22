# -*- coding: utf-8 -*-
"""按需旧账召回：只查明确问起的往事，且只取少量本地证据。

守护的核心不是「能搜到」，而是**不乱搜**：
  · 没有往事线索 / 没有具体内容词 → 不查；
  · 只有人名、没有那件事的消息 → 不拿来凑数；
  · 敏感词、指令行、太老的归档 → 不进 prompt；
  · 结果只放本轮动态层，由 test_harness 钉住。
"""
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "..")

import digest
import recall
from archive import MessageArchive


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = MessageArchive(self.tmp.name, keep_days=0)
        self.now = time.time()
        self.add("u1", "上次说的那杯奶茶真好喝，改天再请你", days=3)
        self.add("u2", "阿强又在吹牛了", days=2)
        self.add("u2", "阿强上次说要请大家吃饭，结果又没下文了", days=2, offset=10)
        self.add("u1", "显卡驱动又崩了，烦死", days=1)
        self.names = ("阿强",)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def add(self, sender, text, days=0, offset=0, **kwargs):
        return self.archive.append(
            "g", sender, text, ts=self.now - days * 86400 + offset, **kwargs)

    def name_of(self, sender):
        return {"u1": "老王", "u2": "阿强"}.get(sender)

    def note(self, text, **kwargs):
        return recall.build_note(
            "g", text, archive=self.archive, names=self.names,
            name_of=self.name_of, now=self.now, **kwargs)

    def test_no_cue_returns_nothing(self):
        self.assertEqual(self.note("今天显卡又崩了"), "")

    def test_cue_without_subject_returns_nothing(self):
        """有往事线索但没有具体事由：「你还记得吗」不该把所有旧账翻出来。"""
        self.assertEqual(self.note("你还记得吗"), "")

    def test_relevant_message_beats_name_only_distractor(self):
        out = self.note("上次阿强说的奶茶是什么来着")
        self.assertIn("奶茶", out)
        self.assertNotIn("请大家吃饭", out)

    def test_name_only_fallback_requires_substantive_message(self):
        out = self.note("上次阿强说的那件事")
        self.assertIn("请大家吃饭", out)
        self.assertNotIn("又在吹牛", out, "太短的名字提及不该占用名额")

    def test_lookback_excludes_old_rows(self):
        self.add("u1", "显卡驱动去年也崩过一次", days=40)
        out = self.note("之前说的显卡", lookback_days=30)
        self.assertIn("烦死", out)
        self.assertNotIn("去年", out)

    def test_cmd_rows_are_not_evidence(self):
        self.add("u1", "显卡模式启动命令记录", days=0, cmd=True)
        out = self.note("之前说的显卡")
        self.assertNotIn("模式启动", out)

    def test_current_message_is_excluded(self):
        text = "之前说的量子计算机项目又延期了"
        self.add("u1", text, days=0)
        out = self.note(text, exclude_text=text)
        self.assertEqual(out, "", "只有当前这句时不应召回自己")

    def test_sensitive_snippet_is_skipped(self):
        self.add("u2", "阿强上次说主席那个事，大家都别接", days=3)
        out = self.note("阿强上次说的那件事")
        self.assertNotIn("主席", out)
        self.assertIn("请大家吃饭", out)

    def test_sensitive_query_returns_nothing(self):
        self.assertEqual(self.note("上次说的主席"), "")

    def test_raw_machine_id_is_scrubbed(self):
        self.add("u1", "量子计算报错 0123456789ABCDEF0123456789ABCDEF", days=0)
        out = self.note("之前说的量子")
        self.assertIn("群友编号", out)
        self.assertNotIn("0123456789ABCDEF", out)

    def test_promise_is_included_when_asked(self):
        book = digest.PromiseBook()
        book.sync("g", [{"who": "阿强", "what": "请全群喝奶茶"}], now=self.now)
        out = recall.build_note(
            "g", "阿强之前答应请奶茶", archive=self.archive, promises=book,
            names=self.names, name_of=self.name_of, now=self.now)
        self.assertIn("承诺", out)
        self.assertIn("请全群喝奶茶", out)
        self.assertIn("奶茶", out)

    def test_max_items_is_enforced(self):
        for i in range(4):
            self.add("u%d" % i, "显卡又出问题了，第%d次" % i, days=0, offset=i)
        out = self.note("之前说的显卡", max_items=1)
        self.assertEqual(out.count("\n- "), 1)

    def test_header_carries_guardrails(self):
        out = self.note("之前说的显卡")
        self.assertIn(recall.RECALL_MARK, out)
        self.assertIn("不要主动提旧事", out)
        self.assertIn("别把别人的事安到眼前这位头上", out)

    def test_bot_wires_recall_into_group_replies(self):
        import inspect
        import bot
        src = inspect.getsource(bot)
        self.assertIn("recall_mod.build_note", src)
        self.assertIn("recall_note=recall_note", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
