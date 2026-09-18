"""改名节流 —— 一次围攻能试几次，才是真正的防线。

由来（2026-09-18，一个下午的真实围攻）
----------------------------------------
13 分钟里群友轮番用谐音称呼撞起名接口，结果是：

    审核拦下 10 个：蒋清 / 习远平 / 湖景滔 / 章国焘 / 章村樵 / 臧椿瞧 / 摇闻远 / 耀问圆 / 西津评 / 遥瘟苑
    审核放过  7 个：尧闻原 / 凌标 / 网红闻 / 王玉瑶 / 樟蝽鞘 / 王宇遥 / 留清姗

回头看，**漏过去的并不比拦下的更干净**，它们只是「这一次没被抽中」的那一发。
那 10 次里面还有一个细节：当天 Gemini 大半时间在报地区 400，审核实际上多半由备用供应商跑，
而替代者在判断这类伪装上明显更松 —— 但这个结论本身不重要，**重要的是它随时可能再变**。

所以这里不追求把模型调到全对（调不到），而是补一层本地节流：
  · 个人层 —— 同一个名字改过之后要隔一会儿才能再改。正常人一天改一次都算多，
    「刚记下就马上换下一个」正是试名字的标准动作。
  · 群层 —— 窗口里累计拦下这么多次，就说明有人在挠这里，整群进入改名冷静期。
    单人间隔摁不住「换个人接着试」，这一层是兜那个底的。

把一次围攻能试的次数从十几次压到个位数，漏判的概率就跟着降一个数量级 ——
它不依赖任何模型当天心情好不好，也不花 token。
"""
import copy
import sys
import unittest

sys.path.insert(0, "..")

import bot  # noqa: E402
import wordfilter  # noqa: E402

GID = "TESTGID_NICK_FLOOD"
OWNER = "OPENID_NF_OWNER"
A = "OPENID_NF_A"
B = "OPENID_NF_B"


class ThrottleRuleTest(unittest.TestCase):
    """闸门本身的算法。时间全部显式传入，不用真的等。"""

    def setUp(self):
        self.saved = copy.deepcopy(bot.NICK_FLOOD)
        self.orig = (bot.NICK_FLOOD, bot._nick_rejects, bot._nick_cool, bot._nick_done)
        bot.reset_nick_flood()
        bot.NICK_FLOOD = copy.deepcopy(self.saved)
        bot.NICK_FLOOD["self_gap"] = 180.0

    def tearDown(self):
        bot.NICK_FLOOD = self.saved
        bot.reset_nick_flood()

    def test_a_group_with_no_history_is_open(self):
        self.assertIsNone(bot._nick_block_reason(GID, A, now=1000.0))

    def test_two_rejects_do_not_freeze_the_group_yet(self):
        self.assertFalse(bot._nick_note_reject(GID, now=1000.0))
        self.assertFalse(bot._nick_note_reject(GID, now=1010.0))
        self.assertIsNone(bot._nick_block_reason(GID, A, now=1011.0),
                          "才两次就封，正常人会以为功能坏了")

    def test_third_reject_freezes_the_whole_group(self):
        bot.NICK_FLOOD["max_rejects"] = 3
        bot._nick_note_reject(GID, now=1000.0)
        bot._nick_note_reject(GID, now=1010.0)
        self.assertTrue(bot._nick_note_reject(GID, now=1020.0), "第三次该触发冷静期了")

        hit = bot._nick_block_reason(GID, A, now=1030.0)
        self.assertIsNotNone(hit, "冷静期里居然放行了")
        self.assertIn("分钟", hit, "得告诉人等多久，不然他会一直重试")

    def test_cooldown_expires(self):
        bot.NICK_FLOOD["max_rejects"] = 1
        bot.NICK_FLOOD["cooldown"] = 600.0
        bot._nick_note_reject(GID, now=1000.0)
        self.assertIsNotNone(bot._nick_block_reason(GID, A, now=1100.0))
        self.assertIsNone(bot._nick_block_reason(GID, A, now=1700.0),
                          "冷静期过了就该恢复正常")

    def test_old_rejects_fall_out_of_the_window(self):
        bot.NICK_FLOOD["max_rejects"] = 3
        bot._nick_note_reject(GID, now=0.0)
        bot._nick_note_reject(GID, now=10.0)
        # 隔了整整一个窗口之后，前面的账已经不算了
        self.assertFalse(bot._nick_note_reject(GID, now=5000.0))

    def test_the_owner_is_exempt_from_the_group_freeze(self):
        """群主的手不该被别人的火中取栗绑住 —— 他本来就有全群的改名权。"""
        bot.NICK_FLOOD["max_rejects"] = 1
        bot._nick_note_reject(GID, now=1000.0)
        self.assertIsNone(bot._nick_block_reason(GID, A, is_owner=True, now=1010.0))
        self.assertIsNotNone(bot._nick_block_reason(GID, A, is_owner=False, now=1010.0))

    def test_the_same_person_cannot_rattle_off_names(self):
        bot._nick_note_accept(GID, A, now=1000.0)
        self.assertIsNotNone(bot._nick_block_reason(GID, A, now=1050.0),
                             "刚改完就能再改，等于让人无限试")
        self.assertIsNone(bot._nick_block_reason(GID, A, now=1000.0 + 181.0))

    def test_the_gap_is_per_name_not_per_group(self):
        """群主连着给**不同的人**改名不该被间隔卡住 —— 那是他的正常用法。"""
        bot._nick_note_accept(GID, A, now=1000.0)
        self.assertIsNone(bot._nick_block_reason(GID, B, now=1001.0))
        self.assertIsNotNone(bot._nick_block_reason(GID, A, now=1001.0))


class ThrottleWiringTest(unittest.IsolatedAsyncioTestCase):
    """闸门得真的接在改名这条路上 —— 逻辑孤岛在这个项目里吃过亏。"""

    def setUp(self):
        self.saved_flood = copy.deepcopy(bot.NICK_FLOOD)
        self.orig_reply = bot.safe_reply
        self.orig_judge = bot.judge_nick
        self.sent = []
        self.judged = []

        async def fake_reply(message, text, **_kw):
            self.sent.append(text)

        async def fake_judge(nick):
            self.judged.append(nick)
            return False            # 一律拦下，模拟一轮持续失败的围攻

        bot.safe_reply = fake_reply
        bot.judge_nick = fake_judge
        bot.reset_nick_flood()
        bot.NICK_FLOOD = copy.deepcopy(self.saved_flood)
        bot.NICK_FLOOD["max_rejects"] = 3
        bot.NICK_FLOOD["self_gap"] = 0.0        # 这一组只测群级冷静期
        self.saved_owner = bot.OWNER_OPENID
        bot.OWNER_OPENID = OWNER

    def tearDown(self):
        bot.safe_reply = self.orig_reply
        bot.judge_nick = self.orig_judge
        bot.OWNER_OPENID = self.saved_owner
        bot.NICK_FLOOD = self.saved_flood
        bot.reset_nick_flood()
        prefix = f"{GID}|"
        for k in [k for k in list(bot.RELATIONS.records) if k.startswith(prefix)]:
            bot.RELATIONS.records.pop(k, None)
        bot.RENAMES.groups.pop(GID, None)
        bot.STATE.data.get("groups", {}).pop(GID, None)

    async def _ask(self, nick, sender=A, is_owner=False, mentioned=()):
        await bot.handle_nick_command(
            None, {"scope": "self", "nick": nick}, GID, sender, is_owner, list(mentioned))

    def _last(self):
        return self.sent[-1] if self.sent else ""

    async def test_repeated_bad_names_freeze_the_group(self):
        for n in ("甲一", "乙二", "丙三"):
            await self._ask(n)
        self.assertIn("我不敢往上写", self._last())

        await self._ask("丁四")
        self.assertEqual(len(self.judged), 3,
                         "冷静期里还在调模型 —— 节流白写了，也没省下 token")
        self.assertIn("分钟", self._last(), "被拦下了得把话说出来，不能装没听见")

    async def test_a_member_cannot_sidestep_it_on_a_clean_name(self):
        """干净名字也照样当面前的闸门 —— 节流看的是「试了几次」，不是「这名字脏不脏」。"""
        for n in ("甲一", "乙二", "丙三"):
            await self._ask(n)
        before = len(self.sent)
        await self._ask("阿强")
        self.assertEqual(len(self.judged), 3)
        self.assertIn("分钟", self._last())
        self.assertIsNone(
            (bot.RELATIONS.get(GID, A, create=False) or {}).get("nick"),
            "被挡住了却不许写档进去了")

    async def test_the_owner_can_still_rename_during_a_freeze(self):
        for n in ("甲一", "乙二", "丙三"):
            await self._ask(n)

        async def ok_judge(nick):
            self.judged.append(nick)
            return None             # 放行

        bot.judge_nick = ok_judge
        await bot.handle_nick_command(
            None, {"scope": "other", "nick": "阿澈"}, GID, OWNER, True, [B])
        self.assertEqual((bot.RELATIONS.get(GID, B, create=False) or {}).get("nick"), "阿澈",
                         "群主在冷静期里被卡住了 —— 他的改名权不该受影响")

    async def test_name_collisions_do_not_count_as_an_attack(self):
        """重名只是业务冲突，拿来当「有人在攻击」的证据会冤枉好人。"""
        bot.judge_nick = AsyncNoneJudge(self)
        bot.RELATIONS.set_nick(GID, B, "阿强", source="claim")
        await self._ask("阿强")
        self.assertIn("已经有人叫这个", self._last())
        for i in range(3):
            await self._ask(f"重名测试{i}")
        self.assertEqual(bot._nick_rejects.get(GID, []), [], "重名被算进围攻统计了")

    async def test_a_sensitive_word_hit_counts_as_an_attack(self):
        """撞本地敏感词是最硬的证据，当然要计数。"""
        wordfilter.add_runtime_words(["典中典暗号测试"])
        try:
            for i in range(3):
                await self._ask("典中典暗号测试")
            self.assertIsNotNone(bot._nick_cool.get(GID), "敏感词命中没触发冷静期")
        finally:
            bot.wordfilter._runtime.discard("典中典暗号测试")

    async def test_successful_rename_starts_the_personal_gap(self):
        bot.NICK_FLOOD["self_gap"] = 180.0
        bot.judge_nick = AsyncNoneJudge(self)

        await self._ask("阿强")
        self.assertIn("记住了", self._last())

        await self._ask("阿远")
        self.assertIn("再等", self._last(), "刚改完立刻再改，应该被间隔挡住")
        self.assertEqual(
            (bot.RELATIONS.get(GID, A, create=False) or {}).get("nick"), "阿强",
            "被间隔挡住了，名字就不该被改掉")

    async def test_the_reply_after_a_block_is_never_silent(self):
        """节流也是一种「没办成」，同样不许让人对着空气喊。"""
        bot.NICK_FLOOD["self_gap"] = 180.0
        bot.judge_nick = AsyncNoneJudge(self)
        await self._ask("阿强")
        bot.NICK_FLOOD["window"] = 600.0
        bot._nick_note_reject(GID)
        bot._nick_note_reject(GID)
        bot._nick_note_reject(GID)
        await self._ask("阿远")
        self.assertTrue(self.sent, "挡住了却不回话，用户只会以为机器人死机了")
        self.assertIn("歇", self._last())


class AsyncNoneJudge:
    """一律放行，`judged` 记一笔，方便断言有没有多花调用。"""

    def __init__(self, case):
        self.case = case

    async def __call__(self, nick):
        self.case.judged.append(nick)
        return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
