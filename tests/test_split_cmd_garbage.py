# -*- coding: utf-8 -*-
"""split_cmd 对打坏的协议标签的容错（2026-10-15 线上实测）。

模型偶尔把记账标签打坏成 `<CMD!` 这类残片——正规正则不匹配，整段连着
`&*%"#$#` 一起漏进群里，群友看到的就是乱码（当天老王回「小王坏掉了」）。
协议规定标签永远在最末尾，所以正文里残留任何 <CMD 片段，从那里砍到结尾，
原文进日志备查。
"""
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402


class SplitCmdGarbageTest(unittest.TestCase):

    def test_malformed_tag_remnant_is_cut(self):
        """`<CMD!` 不匹配正规标签——必须从 <CMD 起截断，不许漏进群里。"""
        body, payload = bot.split_cmd("想卖&*%#想<CMD!卖了换排骨汤，你还没那2斤肉沉")
        self.assertNotIn("<CMD", body)
        self.assertNotIn("卖了换排骨汤", body, "残片之后的内容一并砍掉")
        self.assertIsNone(payload)

    def test_wellformed_tag_still_parsed(self):
        """正规路径不许被新截断逻辑误伤。"""
        body, payload = bot.split_cmd("行吧<CMD>{\"aff\":-1}</CMD>")
        self.assertEqual(body, "行吧")
        self.assertEqual(payload, {"aff": -1})

    def test_clean_text_untouched(self):
        body, payload = bot.split_cmd("正常说话，没有标签")
        self.assertEqual(body, "正常说话，没有标签")
        self.assertIsNone(payload)


class GarbageRunDetectorTest(unittest.TestCase):
    """采样乱码检测器：两个历史形态都命中，正常缩写不误伤。"""

    def test_hex_percent_shape_is_caught(self):
        run = bot._garbage_run("哥哥0A9F%F5块钱的那个瀑布过滤算不算")
        self.assertIsNotNone(run)
        self.assertIn("%", run)

    def test_symbol_run_shape_is_caught(self):
        self.assertIsNotNone(bot._garbage_run("想卖&*%#想说话"))

    def test_normal_abbreviations_pass(self):
        for text in ("你这话把我 CPU 干烧了", "我加92的", "这开关带1080P吗",
                     "boss真难打", "GG了兄弟们", "问个正经问题"):
            self.assertIsNone(bot._garbage_run(text), text)

    def test_long_model_numbers_are_accepted_false_positive(self):
        """RTX4090 这类长型号会误报——设计上只重试一次不吞回复，如实钉住。"""
        self.assertIsNotNone(bot._garbage_run("你这卡是RTX4090吧"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
