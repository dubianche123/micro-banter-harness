"""QQ delivery receipts and bounded retry queue shared by every reply path."""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import logging
import time
from typing import Callable

import aiohttp
from botpy.message import C2CMessage

logger = logging.getLogger("qqbot")
GROUP_PASSIVE_WINDOW_SECONDS = 240
C2C_PASSIVE_WINDOW_SECONDS = 3000


def passive_reply_window(message):
    return C2C_PASSIVE_WINDOW_SECONDS if isinstance(message, C2CMessage) else GROUP_PASSIVE_WINDOW_SECONDS


def message_age_seconds(message):
    raw = getattr(message, "timestamp", None)
    if not raw:
        return None
    try:
        sent = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(timezone.utc) if sent.tzinfo else datetime.now()
    return (now - sent).total_seconds()


def connection_error(error):
    return isinstance(error, (ConnectionError, asyncio.TimeoutError,
                              aiohttp.ClientConnectionError))



@dataclass(frozen=True)
class DeliveryResult:
    status: str
    text: str
    receipt: object = None
    reason: str = ""

    @property
    def delivered(self):
        return self.status == "delivered"


@dataclass
class PendingSend:
    message: object
    text: str
    created_at: float
    sequence: int = 1
    on_delivered: Callable | None = None
    is_current: Callable | None = None
    retry_lock: object = None


class Sender:
    """Only a positive platform receipt commits a reply; retries keep its sequence."""

    def __init__(self, *, fallback=None, max_pending=20, clock=time.time):
        self.pending = deque()
        self.max_pending = max_pending
        self.fallback = fallback
        self.clock = clock
        self._sequences = OrderedDict()
        self._drain_lock = asyncio.Lock()
        self.last_status = None
        self.last_delivery_at = None

    def _next_sequence(self, message):
        key = (getattr(message, "group_openid", None), getattr(message, "id", id(message)))
        value = self._sequences.pop(key, 0) + 1
        self._sequences[key] = value
        if len(self._sequences) > 2000:
            self._sequences.popitem(last=False)
        return value

    async def _delivered(self, item, receipt):
        result = DeliveryResult("delivered", item.text, receipt)
        self.last_status = result.status
        self.last_delivery_at = self.clock()
        logger.info("📤 [已送达] %s", item.text.replace("\n", " ")[:120])
        if item.on_delivered is not None:
            try:
                pending = item.on_delivered(result)
                if inspect.isawaitable(pending):
                    await pending
            except Exception:
                # A failed state callback must never resend a message already delivered.
                logger.exception("发送已确认，但提交回复状态失败")
        return result

    def _queue(self, item, reason):
        if len(self.pending) >= self.max_pending:
            self.pending.popleft()
            logger.warning("补投队列已满，撤下最早的未确认回复")
        self.pending.append(item)
        self.last_status = "queued"
        return DeliveryResult("queued", item.text, reason=reason)

    async def reply(self, message, text, *, on_delivered=None, is_current=None, retry_lock=None):
        item = PendingSend(message, text, self.clock(), self._next_sequence(message),
                           on_delivered, is_current, retry_lock)
        if is_current and not is_current():
            return DeliveryResult("superseded", text)
        age = message_age_seconds(message)
        if age is not None and age > passive_reply_window(message):
            self.last_status = "expired"
            return DeliveryResult("expired", text, reason="passive reply window elapsed")
        for attempt in range(2):
            if item.is_current and not item.is_current():
                return DeliveryResult("superseded", item.text)
            age = message_age_seconds(message)
            if age is not None and age > passive_reply_window(message):
                self.last_status = "expired"
                return DeliveryResult("expired", item.text, reason="passive reply window elapsed")
            try:
                sent = await message.reply(content=item.text, msg_type=0, msg_seq=item.sequence)
            except Exception as error:
                if connection_error(error):
                    return self._queue(item, type(error).__name__)
                if attempt == 0 and self.fallback and ("40034006" in str(error) or "违规" in str(error)):
                    item.text = self.fallback(message)
                    item.sequence = self._next_sequence(message)
                    continue
                self.last_status = "rejected"
                logger.warning("QQ拒收回复: %s", str(error)[:160])
                return DeliveryResult("rejected", item.text, reason=type(error).__name__)
            if sent:
                return await self._delivered(item, sent)
            # botpy can swallow an HTTP timeout. Reuse the same sequence once.
        return self._queue(item, "unconfirmed timeout")

    async def drain(self):
        async with self._drain_lock:
            count = len(self.pending)
            for _ in range(count):
                item = self.pending.popleft()
                async with item.retry_lock or nullcontext():
                    if item.is_current and not item.is_current():
                        self.last_status = "superseded"
                        continue
                    age = message_age_seconds(item.message)
                    if (self.clock() - item.created_at > passive_reply_window(item.message) - 15
                            or (age is not None and age > passive_reply_window(item.message))):
                        self.last_status = "expired"
                        continue
                    try:
                        sent = await item.message.reply(content=item.text, msg_type=0, msg_seq=item.sequence)
                    except Exception as error:
                        if connection_error(error):
                            self.pending.appendleft(item)
                            break
                        self.last_status = "rejected"
                        continue
                    if sent:
                        await self._delivered(item, sent)
                    else:
                        self.pending.append(item)

    def status(self):
        return {"queued": len(self.pending), "last_status": self.last_status,
                "last_delivery_at": self.last_delivery_at}
