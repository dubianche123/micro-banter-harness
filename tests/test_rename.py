"""改名同步 + 模式切换清历史的回归测试。

对应今天暴露的两个现象：
  · 改完名机器人还叫旧名（历史/长期记忆里留着旧称呼）
  · 切了模式只带一点风味就回去了（旧语气还在会话窗口里）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import SessionStore          # noqa: E402
from digest import GroupDigest            # noqa: E402

GROUP = "GROUP_A"
OTHER_GROUP = "GROUP_B"


class ClearGroupTest(unittest.TestCase):
    def test_only_target_group_is_cleared(self):
        s = SessionStore()
        s.record(f"{GROUP}_u1", "你好", "哥们好啊")
        s.record(f"{GROUP}_u2", "在吗", "在的在的")
        s.record(f"{OTHER_GROUP}_u1", "哈喽", "来了老弟")
        self.assertEqual(s.clear_group(GROUP), 2)
        self.assertNotIn(f"{GROUP}_u1", s._sessions)
        self.assertIn(f"{OTHER_GROUP}_u1", s._sessions)

    def test_clear_empty_group_is_safe(self):
        s = SessionStore()
        self.assertEqual(s.clear_group("NEVER_SEEN"), 0)


class RenameUserTest(unittest.TestCase):
    def test_rewrites_history(self):
        s = SessionStore()
        s.record(f"{GROUP}_u1", "机器人叫我阿龙", "记住了，以后叫你【阿龙】")
        hits = s.rename_user(GROUP, "阿龙", "小满")
        self.assertGreaterEqual(hits, 1)
        joined = " ".join(m["content"] for m in s._sessions[f"{GROUP}_u1"]["messages"])
        self.assertNotIn("阿龙", joined)
        self.assertIn("小满", joined)

    def test_single_char_name_not_replaced(self):
        """单字名字替换起来误伤太大，宁可不换。"""
        s = SessionStore()
        s.record(f"{GROUP}_u1", "你好", "你好啊")
        self.assertEqual(s.rename_user(GROUP, "颖", "小满"), 0)

    def test_other_group_untouched(self):
        s = SessionStore()
        s.record(f"{OTHER_GROUP}_u1", "阿龙来了", "阿龙你好")
        self.assertEqual(s.rename_user(GROUP, "阿龙", "小满"), 0)


class RenameInMemoryTest(unittest.TestCase):
    def test_rewrites_summary_text_and_data(self):
        d = GroupDigest()
        d.touch(GROUP)
        d.groups[GROUP]["summary"] = {
            "brief": "阿龙今天在群里认爹，阿龙说要换模型。",
            "data": {
                "topics": [{"topic": "阿龙认爹", "who": ["阿龙"], "heat": 3}],
                "people": [{"who": "阿龙", "note": "自称机器人的爹"}],
            },
        }
        hits = d.rename_in_memory(GROUP, "阿龙", "小满")
        summary = d.get(GROUP)
        self.assertGreaterEqual(hits, 4)
        self.assertIn("小满", summary["brief"])
        self.assertNotIn("阿龙", summary["brief"])
        self.assertEqual(summary["data"]["people"][0]["who"], "小满")

    def test_no_summary_is_safe(self):
        d = GroupDigest()
        self.assertEqual(d.rename_in_memory(GROUP, "阿龙", "小满"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
