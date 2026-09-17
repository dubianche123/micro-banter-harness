"""被动回复「时效闸门」的回归测试。

来由（2026-09-17 线上实测）：机器人掉线那几分钟里群友发的消息积压在腾讯网关侧，
重连时补推过来 —— 这时再去被动回复，腾讯直接拒 `40034005 回复消息msg_id已过期`。
模型跑了、答案也有了，就是发不出去，群里一片安静，看起来跟宕机一样。
所以发之前先看 `message.timestamp` 判断这条是不是太老。

官方口径（QQ 开放平台「消息收发概述」）：**群聊 5 分钟 / 每条最多 5 次，
单聊 60 分钟 / 4 次**。代码里各留了余量 —— 贴着边界回过去照样会被拒。
所以下面有一条专门钉住「单聊不能用群聊的窄窗口卡」，那会把本来能回的消息吞掉。
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "..")

from botpy.message import C2CMessage, GroupMessage  # noqa: E402

import bot  # noqa: E402

GROUP = "TESTGROUP_REPLYWINDOW"
USER = "TESTUSER_REPLYWINDOW"


def _stamp(seconds_ago):
    """造一个跟 QQ 事件同格式的 RFC3339 时间戳（带本地偏移）。"""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).astimezone().isoformat()


class _ReplyRecorder:
    """只做一件事：把「回复到底发出去没有」记下来。

    放在继承列表**前面**，好盖掉 botpy `GroupMessage.reply`（它会去打真实 API）。
    """

    def __init__(self):
        self.sent = []

    async def reply(self, **kwargs):
        self.sent.append(kwargs)
        return {"id": "fake-reply-id"}


class FakeGroupMessage(_ReplyRecorder, GroupMessage):
    """真的 GroupMessage 子类 —— 过闸门时走的 isinstance 判断跟线上完全一致。"""

    def __init__(self, timestamp, msg_id="MSG_GROUP"):
        GroupMessage.__init__(self, None, None, {
            "id": msg_id,
            "timestamp": timestamp,
            "group_openid": GROUP,
            "author": {"member_openid": USER},
        })
        _ReplyRecorder.__init__(self)


class FakeC2CMessage(_ReplyRecorder, C2CMessage):
    """单聊消息：时效口径和群聊不一样，必须分开测。"""

    def __init__(self, timestamp, msg_id="MSG_C2C"):
        C2CMessage.__init__(self, None, None, {
            "id": msg_id,
            "timestamp": timestamp,
            "author": {"user_openid": USER},
        })
        _ReplyRecorder.__init__(self)


class ReplyWindowTest(unittest.IsolatedAsyncioTestCase):

    async def test_fresh_group_message_goes_out(self):
        """刚发的消息照常回 —— 闸门只拦陈年消息，不能误伤正常对话。"""
        m = FakeGroupMessage(_stamp(1))
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    async def test_stale_group_message_is_not_sent(self):
        """掉线期间积压、重连补推的老消息：不发，也不抛异常。"""
        m = FakeGroupMessage(_stamp(bot.GROUP_PASSIVE_WINDOW_SECONDS + 60))
        await bot.safe_reply(m, "在的")
        self.assertEqual(m.sent, [])

    def test_group_window_stays_inside_the_platform_limit(self):
        """官方群聊上限 5 分钟；我们的窗口必须更紧，否则等于没设。"""
        self.assertGreater(bot.GROUP_PASSIVE_WINDOW_SECONDS, 0)
        self.assertLess(bot.GROUP_PASSIVE_WINDOW_SECONDS, 5 * 60)

    async def test_just_inside_the_window_is_still_sent(self):
        m = FakeGroupMessage(_stamp(bot.GROUP_PASSIVE_WINDOW_SECONDS - 5))
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    async def test_private_chat_window_is_longer(self):
        """单聊官方是 60 分钟。拿群聊的窄窗口去卡单聊，会把本来能回的消息吞掉。"""
        self.assertGreater(bot.C2C_PASSIVE_WINDOW_SECONDS, bot.GROUP_PASSIVE_WINDOW_SECONDS)
        m = FakeC2CMessage(_stamp(10 * 60))
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    async def test_private_chat_also_has_a_ceiling(self):
        """单聊也不是无限期，太老的同样别发。"""
        m = FakeC2CMessage(_stamp(bot.C2C_PASSIVE_WINDOW_SECONDS + 60))
        await bot.safe_reply(m, "在的")
        self.assertEqual(m.sent, [])

    async def test_missing_timestamp_keeps_old_behaviour(self):
        """拿不到时间戳就别拦 —— 宁可发出去被拒，也别静默吞掉回复。"""
        m = FakeGroupMessage(None)
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    async def test_unparsable_timestamp_keeps_old_behaviour(self):
        m = FakeGroupMessage("不是时间")
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    async def test_naive_timestamp_is_read_as_local_time(self):
        """没带时区的时间戳按本地算；当成 UTC 会凭空老 8 小时，把回复全吞了。"""
        m = FakeGroupMessage(datetime.now().isoformat(timespec="seconds"))
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    async def test_utc_z_suffix_is_understood(self):
        """QQ 也可能给 `...Z` 结尾的 UTC 串，不能因为解不开就当成老消息。"""
        now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        m = FakeGroupMessage(now_utc)
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(m.sent), 1)

    def test_age_helper_reports_roughly_right(self):
        """时间差得算准，不然闸门的判断就跟随机差不多。"""
        age = bot.message_age_seconds(FakeGroupMessage(_stamp(120)))
        self.assertAlmostEqual(age, 120, delta=10)

    async def test_content_filter_downgrade_still_works(self):
        """闸门是加在风控降级**之前**的一道独立关卡，不能把降级路径挤掉。"""
        m = FakeGroupMessage(_stamp(1))
        calls = []

        async def fake_reply(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("40034006 消息包含违规内容")
            return {"id": "ok"}

        m.reply = fake_reply
        await bot.safe_reply(m, "在的")
        self.assertEqual(len(calls), 2, "第一次被风控拒了之后，应当再补一句")

    async def test_stale_message_never_hits_the_api(self):
        """关键性质：老消息连一次请求都不该打出去（这才是省下那次的全部意义）。"""
        m = FakeGroupMessage(_stamp(30 * 60))
        await bot.safe_reply(m, "在的")
        self.assertEqual(m.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
