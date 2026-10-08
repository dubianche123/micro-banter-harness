"""STATE.load 曾经只认骨架键：骨架之外的一律丢掉。

display_names / renames / affinity_daily / promises_finished 这些由 collector
写进 data 的键，每次重启都被静默清空 —— 显示名、改名册、好感度日额度、催债
墓碑全灭。2026-10-08 催债墓碑因此失效，罗老板又被 @ 了一次酸菜蹄髈。
根修：load 原样保留不认识的键；本文件钉住这条性质。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "..")

import storage


class UnknownKeysSurviveTest(unittest.TestCase):

    def _store(self, path):
        return storage.StateStore(path, save_interval=999)

    def test_unknown_top_level_keys_survive_load(self):
        """文件里有、骨架里没有的键：原样收进来，不许静默丢。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": storage.STATE_VERSION,
                    "promises": {},
                    "promises_finished": {"G": [{"who": "罗老板", "what": "请全群吃酸菜蹄髈"}]},
                    "display_names": {"G|U": "阿强"},
                }, f, ensure_ascii=False)
            st = self._store(path)
            self.assertEqual(st.data["promises_finished"]["G"][0]["who"], "罗老板")
            self.assertEqual(st.data["display_names"]["G|U"], "阿强")

    def test_survives_a_save_and_reload_roundtrip(self):
        """存一遍再读回来还在 —— 墓碑必须跨重启活着。"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            st = self._store(path)
            st.data["promises_finished"] = {"G": [{"who": "罗老板", "what": "请全群吃酸菜蹄髈"}]}
            st.mark_dirty()
            st.save_now()
            st2 = self._store(path)
            self.assertEqual(st2.data["promises_finished"]["G"][0]["who"], "罗老板")

    def test_skeleton_has_a_home_for_tombstones(self):
        """骨架里有 promises_finished 的位置 —— 键的存在不依赖 collector 是否写过。"""
        with tempfile.TemporaryDirectory() as td:
            st = self._store(os.path.join(td, "s.json"))
            self.assertIn("promises_finished", st.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
