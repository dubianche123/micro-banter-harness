# -*- coding: utf-8 -*-
"""人物名册注入 + 单会话清除的回归测试。

守护的场景（2026-09-20 实测）：被封号的是家豪，模型却对家豪说「老王刚解封」——
digest 的人物档案其实记对了，但它**从来没被注入过**；模型只能读【群史记】里
「A 号刚解封就忙着跟 B 贴贴」这种连动长句，读的时候把主语拧了，还把自己说错的
版本存进会话反复引用。修法两条：
1. 人物名册（谁：什么事，一行一人）随长期记忆一起注入 —— 主语想拧都没得拧；
2. 污染只在一个会话窗口里时，用 drop_session 精准清除，不连坐全群。
"""
import sys
import unittest

sys.path.insert(0, "..")

import digest as digest_mod
import storage


class RenderPeopleTest(unittest.TestCase):
    STATE = {
        "brief": "家豪号刚解封就忙着跟老王贴贴飙车。",
        "data": {
            "people": [
                {"who": "家豪", "note": "号刚解封就到处拉着老王和整蛊小王"},
                {"who": "老王", "note": "积极参与各种整活，一边吃瓜一边调戏小王"},
                {"who": "", "note": "没名字的不该出现"},
                {"who": "龙少", "note": ""},
                "这不是字典也不该崩",
            ],
        },
    }

    def test_every_line_is_a_who_what_pair(self):
        out = digest_mod.render_people(self.STATE)
        self.assertIn("- 家豪：号刚解封就到处拉着老王和整蛊小王", out)
        self.assertIn("- 老王：积极参与各种整活，一边吃瓜一边调戏小王", out)

    def test_header_tells_the_model_to_check_names(self):
        out = digest_mod.render_people(self.STATE)
        self.assertIn("谁的条目说的就是谁的事", out)
        self.assertIn("别张冠李戴", out)

    def test_malformed_entries_are_skipped_silently(self):
        out = digest_mod.render_people(self.STATE)
        self.assertNotIn("没名字的不该出现", out)
        self.assertNotIn("这不是字典", out)

    def test_empty_state_renders_nothing(self):
        self.assertEqual(digest_mod.render_people(None), "")
        self.assertEqual(digest_mod.render_people({}), "")
        self.assertEqual(digest_mod.render_people({"data": {"people": []}}), "")

    def test_cap_on_entries(self):
        state = {"data": {"people": [{"who": f"群友{i}", "note": "x"} for i in range(20)]}}
        out = digest_mod.render_people(state, limit=8)
        self.assertEqual(out.count("\n- "), 8)

    def test_people_block_is_not_in_the_group_visible_summary(self):
        """名册只喂给模型；发到群里的摘要（render_summary）不该把档案整段晒出去。"""
        self.assertNotIn("👥", digest_mod.render_summary(self.STATE))


class PeopleBlockReachesTheModelTest(unittest.TestCase):
    """注入接线：人物名册必须拼进 group_memory，和【群史记】一起到模型眼前。"""

    def test_injection_wiring_calls_render_people(self):
        import inspect
        import bot
        src = inspect.getsource(bot)
        self.assertIn("render_people", src, "bot.py 没有注入人物名册")


class DropSessionTest(unittest.TestCase):
    def setUp(self):
        self.store = storage.SessionStore(max_turns=4, max_chars=2000, ttl=3600)

    def test_drops_exactly_one_session(self):
        self.store.record("g1_a", "hi", "ho")
        self.store.record("g1_b", "hi", "ho")
        self.assertTrue(self.store.drop_session("g1_a"))
        self.assertNotIn("g1_a", self.store._sessions)
        self.assertIn("g1_b", self.store._sessions, "别的会话不许被连坐")

    def test_missing_session_returns_false(self):
        self.assertFalse(self.store.drop_session("nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
