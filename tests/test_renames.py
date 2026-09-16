"""旧称呼台账的回归测试。

要守住的只有一条：**喂给模型的文本里，一个人永远只有当前这一个称呼。**
改过名就换成新的，撤销了就退成「（未留名·XXXX）」，改了多少次都不许留下旧字面 ——
否则模型会拿旧名去叫人，甚至把旧名和新名当成两个人。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relations import RenameLedger  # noqa: E402

G = "GROUP_X"


class RenameLedgerTest(unittest.TestCase):
    def setUp(self):
        self.led = RenameLedger()
        self.labels = {"OPENID_A": "阿强", "OPENID_B": "小满"}

    def label_of(self, openid):
        return self.labels.get(openid)

    def test_old_name_becomes_current_name(self):
        """改了名，历史文本里的旧称呼要跟着变成新称呼。"""
        self.led.note(G, "阿龙", "OPENID_A")
        out = self.led.refresh(G, "阿龙昨天又迟到了", label_of=self.label_of)
        self.assertEqual(out, "阿强昨天又迟到了")
        self.assertNotIn("阿龙", out)

    def test_revoked_name_falls_back_to_placeholder(self):
        """撤销了称呼，旧名不该再出现 —— 退成「未留名」，谁都不许再被这么叫。"""
        self.led.note(G, "爸爸", "OPENID_A")
        self.labels["OPENID_A"] = None
        out = self.led.refresh(G, "爸爸来了", label_of=self.label_of)
        self.assertEqual(out, "（未留名·ID_A）来了")

    def test_current_owner_of_the_name_is_untouched(self):
        """旧名又被别人认领了，就说明它现在有主 —— 不许再把它的历史算到前一个人头上。"""
        self.led.note(G, "阿龙", "OPENID_A")
        out = self.led.refresh(G, "阿龙在吗", label_of=self.label_of,
                               current_names=["阿龙", "阿强"])
        self.assertEqual(out, "阿龙在吗")

    def test_chained_rename_resolves_to_latest(self):
        """A → B → C 连环改名，文本里最老的那个名字也该落到最新称呼上。"""
        self.led.note(G, "阿强", "OPENID_A")
        self.labels["OPENID_A"] = "老李"
        out = self.led.refresh(G, "阿强和阿龙", label_of=self.label_of)
        self.assertEqual(out, "老李和阿龙")

    def test_claiming_a_name_drops_the_previous_record(self):
        """同一个名字换了主人，旧账当场作废。"""
        self.led.note(G, "阿龙", "OPENID_A")
        self.led.note(G, "阿龙", "OPENID_B")
        out = self.led.refresh(G, "阿龙来了", label_of=self.label_of)
        self.assertEqual(out, "小满来了")

    def test_single_char_names_are_not_recorded(self):
        """一个字的名字不记台账：叫「澈」的人一改名，全群的「清澈」「澄澈」都会被改写。"""
        self.assertFalse(self.led.note(G, "王", "OPENID_A"))
        self.assertEqual(self.led.refresh(G, "王五来了", label_of=self.label_of), "王五来了")

    def test_empty_inputs_are_safe(self):
        self.assertEqual(self.led.refresh(G, ""), "")
        self.assertFalse(self.led.note(G, "", "OPENID_A"))
        self.assertFalse(self.led.note(G, "阿龙", ""))
        self.assertEqual(self.led.refresh("没登记的群", "阿龙", label_of=self.label_of), "阿龙")

    def test_survives_restart(self):
        """台账要能落盘、能恢复 —— 重启之后旧名照样不该漏出去。"""
        self.led.note(G, "阿龙", "OPENID_A")
        data = {}
        self.led.dump_into(data)
        self.assertEqual(data["renames"][G]["阿龙"], "OPENID_A")

        fresh = RenameLedger()
        self.assertEqual(fresh.hydrate(data["renames"]), 1)
        self.assertEqual(fresh.refresh(G, "阿龙来了", label_of=self.label_of), "阿强来了")

    def test_ledger_is_bounded(self):
        """名字换得再勤，台账也不能无限长。"""
        led = RenameLedger(max_items=5)
        for i in range(20):
            led.note(G, f"名字{i}", "OPENID_A")
        self.assertLessEqual(len(led.groups[G]), 5)
        # 被挤掉的是最老的，最近的还在
        self.assertIn("名字19", led.groups[G])


class SharedRulesTest(unittest.TestCase):
    """防幻觉那几条硬规则必须真的进了稳定头 —— 它们没进的话，模型会自己编关系。"""

    def test_rules_mention_kinship_ban(self):
        import prompts
        rules = prompts.PROMPT_SHARED_RULES
        self.assertIn("身份与关系", rules)
        for word in ("爹", "主人", "家谱"):
            self.assertIn(word, rules)

    def test_rules_are_not_copied_into_each_mode(self):
        """这几条不属于任何一个人设：共享一份就够，各模式里不该再抄一遍。"""
        import bot
        for mode, text in bot.MODE_PROMPTS.items():
            self.assertNotIn("身份与关系", text, f"{mode} 不该自己抄一份共享规则")


if __name__ == "__main__":
    unittest.main(verbosity=2)
