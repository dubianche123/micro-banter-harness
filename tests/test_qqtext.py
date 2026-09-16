"""qqtext.normalize 回归测试。

要钉住的不变量只有一条：**读不出人话的内容不许留在正文里**。
它一旦漏出去，会同时污染三处 —— 归档原文、群聊背景缓存、喂给模型的上下文。
"""
import sys
import unittest

sys.path.insert(0, "..")

import qqtext


class NormalizeTest(unittest.TestCase):

    def test_face_with_caption_kept(self):
        """表情的语义藏在 ext 的 base64 里，解得出来就该留下。"""
        raw = '<faceType=1,faceId="0",ext="eyJ0ZXh0Ijoi5oOK6K62In0=">'
        self.assertEqual(qqtext.normalize(raw), "[表情：惊讶]")

    def test_face_without_caption_dropped(self):
        """配文为空的表情读不出任何语义（今天归档里 5 条全是这种），整条丢。"""
        raw = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
        self.assertEqual(qqtext.normalize(raw), "")

    def test_broken_ext_never_raises(self):
        """ext 坏掉也必须安静退化，不能让一条烂消息把整个处理流程炸掉。"""
        for bad in ('<faceType=1,faceId="0",ext="这不是base64">',
                    '<faceType=1,faceId="0",ext="">',
                    '<faceType=1,faceId="0">',
                    '<faceType=1,faceId="0",ext="e30=">'):
            self.assertEqual(qqtext.normalize(bad), "", bad)

    def test_mention_placeholder_is_kept_as_a_name(self):
        """@ 了谁必须留下 —— 这是「谁在跟谁说话」里最关键的信息。

        以前这里整个清掉，代价很实：群友 @ 群主再问「这是谁」，机器人那边只剩一句
        没头没脑的话，答不上来。渲染成「@某人」才既保住了事实又人能读。
        """
        self.assertEqual(
            qqtext.normalize("<@!A1B2C3D4E5>这是谁",
                             mention_label=lambda oid: "阿强"),
            "@阿强这是谁")

    def test_mention_without_resolver_falls_back_to_short_id(self):
        """解析器给不出名字（那人还没认领称呼）也要看得出「这里 @ 了个人」。"""
        self.assertEqual(qqtext.normalize("<@!A1B2C3D4E5>这是谁"), "@群友D4E5这是谁")

    def test_mention_in_the_middle_of_sentence(self):
        """跟内容里那个多余的 @ 不要留成「@@某人」。"""
        out = qqtext.normalize("刚 @<@!AAAABBBB> 说的那个",
                               mention_label=lambda oid: "小雨")
        self.assertEqual(out, "刚 @小雨 说的那个")

    def test_broken_mention_label_never_raises(self):
        """认人的表坏掉不该把整条消息带崩 —— 退成短 ID 就好。"""
        def boom(_oid):
            raise RuntimeError("表炸了")

        self.assertEqual(qqtext.normalize("<@!A1B2C3D4E5>", mention_label=boom),
                         "@群友D4E5")

    def test_plain_nickname_kept(self):
        """明文 @昵称 是人写的，要留住（上层靠它认人、起名）。"""
        self.assertEqual(qqtext.normalize("小星，叫@阿澈 小满"), "小星，叫@阿澈 小满")

    def test_short_text_kept(self):
        """一个问号也是真实的接话信号 —— 别拿长度当尺子。"""
        for text in ("？", "?", "太蠢了日", "ok", "[表情：惊讶]"):
            self.assertEqual(qqtext.normalize(text), text)

    def test_media_line_dropped(self):
        raw = ("[附件1] 类型:图片 文件名:a.jpg 尺寸:882x1920 大小:148.9KB "
               "URL:https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x")
        self.assertEqual(qqtext.normalize(raw), "")

    def test_bare_url_dropped_but_text_kept(self):
        self.assertEqual(qqtext.normalize("看这个 https://example.com/a?b=1 挺好"), "看这个 挺好")

    def test_unknown_tag_dropped(self):
        """认不出的占位符一律清掉，别让模型去读一串乱码。"""
        self.assertEqual(qqtext.normalize('<img src="x">你好'), "你好")

    def test_forward_record_keeps_human_words(self):
        """转发记录里的人话要留，附件元数据要丢。"""
        raw = ("[群聊的聊天记录]\n[消息内容] 哥还是别露脸了\n"
               "[附件1] 类型:图片 文件名:b.jpg URL:https://x.cn/y")
        out = qqtext.normalize(raw)
        self.assertIn("哥还是别露脸了", out)
        self.assertNotIn("b.jpg", out)
        self.assertNotIn("https://", out)

    def test_empty_input(self):
        self.assertEqual(qqtext.normalize(""), "")
        self.assertEqual(qqtext.normalize(None), "")

    def test_renderer_registry_is_extensible(self):
        """加一种占位符 = 注册一个渲染器，不用碰解析逻辑。"""
        @qqtext.renderer("tester")
        def _render(_attrs):
            return "<T>"

        try:
            self.assertEqual(qqtext.normalize("<tester=7>"), "<T>")
            self.assertEqual(qqtext.normalize("<tester=7>说话"), "<T>说话")
        finally:
            qqtext.RENDERERS.pop("tester", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
