"""名字解析层的回归测试。

这一层存在的意义就是「代码里一个具体人名都不留」：机器人自称、群主称呼全部可配。
所以测试也按**性质**断言，不依赖某个人名 —— 换个名字部署，这些用例照样成立。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import naming  # noqa: E402
import prompts  # noqa: E402


class BotNameTest(unittest.TestCase):
    def setUp(self):
        self.saved_platform = naming._platform["name"]

    def tearDown(self):
        naming._platform["name"] = self.saved_platform

    def test_platform_name_joins_the_list(self):
        """群主在 QQ 那边把机器人改名，进程一起来就该认出来（不用改代码、不用改 .env）。"""
        naming.set_platform_name("阿福")
        self.assertIn("阿福", naming.bot_names())

    def test_configured_names_come_first(self):
        """.env 配的别名排在平台昵称前面 —— 自称用第一个，顺序不能反。"""
        naming.set_platform_name("阿福")
        expected = (config.BOT_NAMES or ["阿福"])[0]
        self.assertEqual(naming.bot_name(), expected)

    def test_no_duplicate_when_platform_matches_config(self):
        if not config.BOT_NAMES:
            self.skipTest("没配 BOT_NAMES，测不了去重")
        naming.set_platform_name(config.BOT_NAMES[0])
        self.assertEqual(naming.bot_names().count(config.BOT_NAMES[0]), 1)

    def test_always_falls_back_to_something(self):
        """三样都没有（没配、平台也没给）也要有个自称，不能是空串。"""
        naming.set_platform_name("")
        saved = config.BOT_NAMES
        config.BOT_NAMES = []
        try:
            self.assertEqual(naming.bot_names(), [naming.DEFAULT_BOT_NAME])
        finally:
            config.BOT_NAMES = saved


class OwnerLabelTest(unittest.TestCase):
    def test_claimed_nick_wins(self):
        self.assertEqual(naming.owner_label("老张"), "老张")

    def test_falls_back_to_generic_word(self):
        """群主还没认领称呼时用通用词，绝不凭空编一个名字。"""
        for empty in (None, "", "   "):
            self.assertEqual(naming.owner_label(empty), naming.DEFAULT_OWNER_LABEL)


class RenderTest(unittest.TestCase):
    def test_replaces_both_placeholders(self):
        out = naming.render("你叫“{bot}”，{owner}只是群里一个普通人", bot="阿福", owner="老张")
        self.assertEqual(out, "你叫“阿福”，老张只是群里一个普通人")

    def test_defaults_are_generic(self):
        naming.set_platform_name("")
        saved, config.BOT_NAMES = config.BOT_NAMES, []
        try:
            out = naming.render("{bot}和{owner}")
            self.assertEqual(out, f"{naming.DEFAULT_BOT_NAME}和{naming.DEFAULT_OWNER_LABEL}")
        finally:
            config.BOT_NAMES = saved

    def test_keeps_literal_braces_intact(self):
        """提示词里有 {"aff": 2} 这种字面花括号（结构化输出协议）。

        这就是 render 用 replace 而不是 str.format 的原因 —— format 一碰就抛
        KeyError，而且是运行到那一段才炸，等于把记账协议整个搞坏。
        """
        out = naming.render(prompts.CMD_PROTOCOL + "{owner}", owner="老张")
        self.assertIn('{"aff": 2}', out)
        self.assertFalse(out.endswith("{owner}"))

    def test_all_personas_render_clean(self):
        """所有人设模板都不该留下未替换的占位符。"""
        for name in dir(prompts):
            val = getattr(prompts, name)
            if name.startswith("PROMPT_") and isinstance(val, str):
                out = naming.render(val, bot="阿福", owner="老张")
                with self.subTest(prompt=name):
                    self.assertNotIn("{bot}", out)
                    self.assertNotIn("{owner}", out)

    def test_none_is_safe(self):
        self.assertIsNone(naming.render(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
