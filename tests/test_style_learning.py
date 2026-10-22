# -*- coding: utf-8 -*-
"""轻量表达学习：压缩时顺带收口头禅，计数裁决后才进每日记忆。

它必须同时满足两件事：
  1. 真的从近期群聊里学到东西（不是模型自由发挥）；
  2. 不把一次性发言、敏感词、旧口癖灌成每轮台词（表达块明确写着「可选参考」）。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot
import config
import digest
import prompts


class SanitizeStyleTest(unittest.TestCase):
    def test_phrase_must_appear_twice_in_this_batch(self):
        data = {"style": [
            {"phrase": "绷不住了", "when": "看到离谱操作"},
            {"phrase": "只出现一次", "when": "某次"},
        ]}
        kept = digest.sanitize_style(data, "绷不住了，这操作绷不住了。只出现一次")
        self.assertEqual(kept, 1)
        self.assertEqual(data["style"][0]["phrase"], "绷不住了")

    def test_sensitive_phrase_is_dropped(self):
        data = {"style": [{"phrase": "主席", "when": "聊到"}]}
        self.assertEqual(digest.sanitize_style(data, "主席 主席"), 0)
        self.assertEqual(data["style"], [])

    def test_malformed_and_too_short_are_dropped(self):
        data = {"style": [
            {"phrase": "草", "when": "x"},
            {"phrase": "正常口头禅", "when": "x"},
            "不是字典",
            {"phrase": "正常口头禅", "when": "y"},
        ]}
        digest.sanitize_style(data, "正常口头禅 正常口头禅")
        self.assertEqual([row["phrase"] for row in data["style"]], ["正常口头禅"])

    def test_cap_follows_config(self):
        rows = [{"phrase": "口头禅%d" % i, "when": "x"} for i in range(20)]
        data = {"style": rows}
        transcript = " ".join("口头禅%d" % i for i in range(20)) * 2
        digest.sanitize_style(data, transcript)
        self.assertEqual(len(data["style"]), config.STYLE_NOTE_MAX_ITEMS)


class RenderStyleTest(unittest.TestCase):
    def state(self, rows):
        return {"data": {"style": rows}}

    def test_renders_optional_language_reference(self):
        out = digest.render_style(self.state([
            {"phrase": "绷不住了", "when": "看到离谱操作"},
        ]))
        self.assertIn(prompts.STYLE_MARK, out)
        self.assertIn("绷不住了", out)
        self.assertIn("看到离谱操作", out)
        self.assertIn("可选", out)

    def test_cap_and_malformed_rows(self):
        out = digest.render_style(self.state([
            {"phrase": "口头禅%d" % i, "when": ""} for i in range(20)
        ] + ["坏的"]), limit=3)
        self.assertEqual(out.count("\n- "), 3)

    def test_capitalized_keys_are_tolerated(self):
        out = digest.render_style(self.state([{"Phrase": "绷不住了", "When": "离谱"}]))
        self.assertIn("绷不住了", out)

    def test_empty_returns_nothing(self):
        self.assertEqual(digest.render_style(None), "")
        self.assertEqual(digest.render_style({"data": {}}), "")

    def test_style_is_not_in_group_visible_summary(self):
        out = digest.render_summary(self.state([{"phrase": "绷不住了", "when": "离谱"}]))
        self.assertNotIn("绷不住了", out)


class StyleSchemaTest(unittest.TestCase):
    def test_reduce_prompt_asks_for_style_with_repeat_rule(self):
        self.assertIn('"style"', digest.SYSTEM_REDUCE)
        self.assertIn("出现过两次", digest.SYSTEM_REDUCE)

    def test_normalize_keeps_style(self):
        data = digest.normalize({"style": [{"phrase": "绷不住了", "when": "离谱"}]})
        self.assertEqual(data["style"][0]["phrase"], "绷不住了")

    def test_old_summary_replays_style_for_continuity(self):
        store = digest.GroupDigest()
        store.touch("g")["summary"] = {
            "brief": "",
            "data": {"style": [{"phrase": "绷不住了", "when": "离谱"}]},
        }
        text = store.old_summary_text(store._slot("g"))
        self.assertIn("上一版口头禅", text)
        self.assertIn("绷不住了", text)

    def test_bot_injects_style_into_daily_memory(self):
        import inspect
        src = inspect.getsource(bot)
        self.assertIn("render_style", src, "bot.py 没有把表达学习注入每日记忆")


class CompressStyleTest(unittest.IsolatedAsyncioTestCase):
    async def test_compress_group_filters_style_by_raw_transcript(self):
        async def ask(system, payload):
            return ('Brief<DIGEST>{"people":[],"style":['
                    '{"phrase":"绷不住了","when":"看到离谱操作"},'
                    '{"phrase":"只出现一次","when":""}]}</DIGEST>')

        store = digest.GroupDigest()
        result = await digest.compress_group(store, "g", [
            {"sender": "u", "text": "绷不住了"},
            {"sender": "u", "text": "这操作真绷不住了"},
            {"sender": "u", "text": "只出现一次"},
        ], ask)
        self.assertIsNotNone(result)
        phrases = [row["phrase"] for row in result["data"]["style"]]
        self.assertEqual(phrases, ["绷不住了"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
