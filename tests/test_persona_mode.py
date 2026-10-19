# -*- coding: utf-8 -*-
"""非线性好感增长 + 硬性相处模式的回归测试（2026-10-19 群主定调）。

- 非线性：关系越往上爬越慢（30 分以下全额，往上打折），下跌不缩水。
  原有的衰减/不均匀台阶/日上限照旧——补的是增长端的边际递减。
- 相处模式：硬枚举白名单（损友/知心/护短/捧场），压缩可以提案、代码只认这几个词，
  编造的一律丢弃——漂移在构造上不可能。
"""
import sys
import time
import unittest

sys.path.insert(0, "..")

import relations  # noqa: E402
import runtime  # noqa: E402
from affinity import apply_digest_modes  # noqa: E402
from relations import _growth_scale  # noqa: E402

GROUP = "TESTGROUP_PERSONA"
OID = "TESTUSER_PERSONA00000000000000000"
NICK = "测试昵称"


def _fresh_rec(score=0):
    """造一个「正在正常交往中」的档案：last_seen/interactions 都是活人该有的样子。

    ⚠️ last_seen 不设的话 _settle 会把分数衰减到 0（last_seen=0 = 久未联系的远古档案），
    那是衰减机制的正常工作，不是测试要测的东西。
    """
    rec = runtime.RELATIONS.get(GROUP, OID)
    rec["score"] = score
    rec["pinned"] = False
    rec["last_seen"] = time.time()
    rec["interactions"] = 5
    return rec


class GrowthScaleTest(unittest.TestCase):

    def setUp(self):
        for k in [k for k in runtime.RELATIONS.records if k.startswith(GROUP)]:
            runtime.RELATIONS.records.pop(k, None)

    def tearDown(self):
        for k in [k for k in runtime.RELATIONS.records if k.startswith(GROUP)]:
            runtime.RELATIONS.records.pop(k, None)

    def test_scale_is_piecewise(self):
        self.assertEqual(_growth_scale(10), 1.0)
        self.assertEqual(_growth_scale(40), 0.6)
        self.assertEqual(_growth_scale(70), 0.4)
        self.assertEqual(_growth_scale(90), 0.3)

    def test_growth_slows_down_at_high_score(self):
        """本命档 +3 只算 +1——爬到 100 是真要用心的。"""
        _fresh_rec(90)
        runtime.RELATIONS.apply(GROUP, OID, 3, count=False)
        self.assertEqual(runtime.RELATIONS.get(GROUP, OID)["score"], 91)

    def test_falls_are_never_scaled(self):
        """信任建立慢、崩起来快：负向全程全额。"""
        _fresh_rec(90)
        runtime.RELATIONS.apply(GROUP, OID, -3, count=False)
        self.assertEqual(runtime.RELATIONS.get(GROUP, OID)["score"], 87)

    def test_low_score_grows_full(self):
        _fresh_rec(10)
        runtime.RELATIONS.apply(GROUP, OID, 3, count=False)
        self.assertEqual(runtime.RELATIONS.get(GROUP, OID)["score"], 13)


class PersonaModeTest(unittest.TestCase):

    def setUp(self):
        for k in [k for k in runtime.RELATIONS.records if k.startswith(GROUP)]:
            runtime.RELATIONS.records.pop(k, None)
        _fresh_rec(45)
        runtime.RELATIONS.set_nick(GROUP, OID, NICK, source="claim")

    def tearDown(self):
        for k in [k for k in runtime.RELATIONS.records if k.startswith(GROUP)]:
            runtime.RELATIONS.records.pop(k, None)

    def test_set_mode_whitelist_and_sources(self):
        self.assertIsNotNone(runtime.RELATIONS.set_mode(GROUP, OID, "知心", source="digest"))
        self.assertEqual(runtime.RELATIONS.get(GROUP, OID)["mode"], "知心")
        # 编造的词：拒绝
        self.assertIsNone(runtime.RELATIONS.set_mode(GROUP, OID, "编制外模式", source="owner"))
        self.assertEqual(runtime.RELATIONS.get(GROUP, OID)["mode"], "知心")
        # 非法来源：拒绝（与主名同构——模式不是模型随口玩的东西）
        self.assertIsNone(runtime.RELATIONS.set_mode(GROUP, OID, "护短", source="model"))

    def test_note_renders_the_mode(self):
        runtime.RELATIONS.set_mode(GROUP, OID, "知心", source="digest")
        rec = runtime.RELATIONS.get(GROUP, OID)
        note = relations.build_relation_note(rec, OID, mode="normal")
        self.assertIn("相处模式：少损多听", note)
        self.assertIn("档位定分寸", note)

    def test_note_has_no_mode_line_when_unset(self):
        rec = runtime.RELATIONS.get(GROUP, OID)
        note = relations.build_relation_note(rec, OID, mode="normal")
        self.assertNotIn("相处模式", note)

    def test_apply_digest_modes_applies_whitelist_only(self):
        rows = [
            {"who": NICK, "mode": "知心"},        # 生效
            {"who": NICK, "mode": "编造模式"},     # 白名单外：丢
            {"who": "不存在的人", "mode": "护短"},  # 名字对不上：宁可不设
        ]
        applied, detail = apply_digest_modes(GROUP, rows)
        self.assertEqual(applied, 1)
        self.assertEqual(runtime.RELATIONS.get(GROUP, OID)["mode"], "知心")
        self.assertIn("知心", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
