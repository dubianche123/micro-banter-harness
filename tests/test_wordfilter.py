"""wordfilter 回归测试。

钉住三条不变量：
  1. 通用词（职务尊号、长辈/主子称谓）必须命中 —— 这是两条泄漏路径共用的底线
  2. 普通昵称绝不能被误伤 —— 误杀的代价是「群友想起个正常外号被拒」，比漏判更让人恼火
  3. 外部词表要能热重载（改了文件不用重启），且文件缺失时必须安静退化成空表

外部词表测试全程用临时文件，不碰仓库里那份 sensitive_nicks.txt。
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "..")

import wordfilter


class BuiltinWordsTest(unittest.TestCase):

    def test_title_words_hit(self):
        """职务/尊号是谐音梗的重灾区，必须命中。"""
        for n in ("X主席", "老总书记", "李总理", "大总统", "书记", "陛下", "皇上"):
            with self.subTest(nick=n):
                self.assertTrue(wordfilter.has_hit(n), f"{n!r} 应该命中内置词")

    def test_arrogant_words_hit(self):
        """长辈/主子称谓：起名和钓鱼都爱用这一套。"""
        for n in ("爸爸", "阿强爸爸", "主人", "祖宗", "爷爷", "老爷"):
            with self.subTest(nick=n):
                self.assertTrue(wordfilter.has_hit(n), f"{n!r} 应该命中内置词")

    def test_normal_nicks_never_hit(self):
        """这些是真实群友在用的正常昵称，一个都不许误伤。"""
        for n in ("阿强", "阿澈", "阿龙", "小满", "抽卡必出金", "老张", "小美", ""):
            with self.subTest(nick=n):
                self.assertFalse(wordfilter.has_hit(n), f"{n!r} 不该被拦")

    def test_hits_returns_words_not_bool(self):
        got = wordfilter.hits("大总统和爸爸")
        self.assertIn("总统", got)
        self.assertIn("爸爸", got)


class ScrubTest(unittest.TestCase):

    def test_replaces_in_place(self):
        """就地替换而不是删行：删行会把句子截成半截，看着像坏了。"""
        out = wordfilter.scrub("机器人被要求叫我爸爸")
        self.assertNotIn("爸爸", out)
        self.assertIn("【已隐去】", out)

    def test_keeps_clean_text_untouched(self):
        clean = "大家晚上好，今天谁请客"
        self.assertEqual(wordfilter.scrub(clean), clean)

    def test_none_and_empty_safe(self):
        self.assertIsNone(wordfilter.scrub(None))
        self.assertEqual(wordfilter.scrub(""), "")

    def test_scrub_is_idempotent(self):
        once = wordfilter.scrub("自称大总统")
        self.assertEqual(wordfilter.scrub(once), once)

    def test_longest_word_wins(self):
        """长词要先替换，否则短词会把长词切碎、拼出难看的残留。"""
        words = wordfilter.all_words()
        lengths = [len(w) for w in words]
        self.assertEqual(lengths, sorted(lengths, reverse=True))


class ExternalWordsTest(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        self._saved = wordfilter.DENY_FILE
        wordfilter.DENY_FILE = self.path
        wordfilter.external_words()          # 预热缓存

    def tearDown(self):
        wordfilter.DENY_FILE = self._saved
        try:
            os.unlink(self.path)
        except OSError:
            pass
        wordfilter.external_words()          # 让缓存回到仓库里那份

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        time.sleep(1.1)                      # 保证 mtime 变化（部分文件系统精度到秒）

    def test_missing_file_is_empty(self):
        os.unlink(self.path)
        self.assertEqual(wordfilter.external_words(), ())
        self.assertFalse(wordfilter.has_hit("随便什么词"))

    def test_reads_words_and_skips_comments(self):
        self._write("# 注释行\n\n测试专用词\n")
        self.assertEqual(wordfilter.external_words(), ("测试专用词",))
        self.assertTrue(wordfilter.has_hit("这词里含测试专用词在里面"))

    def test_hot_reload_without_restart(self):
        """这是给使用者省事的关键：加词不该需要重启机器人。"""
        self.assertFalse(wordfilter.has_hit("新来的词儿"))
        self._write("新来的词儿\n")
        self.assertTrue(wordfilter.has_hit("新来的词儿"))

    def test_removal_takes_effect_too(self):
        self._write("临时词\n")
        self.assertTrue(wordfilter.has_hit("临时词"))
        self._write("")
        self.assertFalse(wordfilter.has_hit("临时词"))

    def test_scrub_uses_external_words(self):
        self._write("外部敏感词\n")
        self.assertEqual(wordfilter.scrub("说了外部敏感词"), "说了【已隐去】")


if __name__ == "__main__":
    unittest.main(verbosity=2)
