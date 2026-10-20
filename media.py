"""图片描述的共同入口：共享在途任务、持久化缓存、按源消息 ID 回填。

providers 负责模型链与全局请求预算；这里只负责「同一张图是否需要再读」。
"""
import asyncio
import logging
import time

import config
import providers

logger = logging.getLogger("qqbot")
CAPTION_CACHE_SIZE = 300


class CaptionService:
    def __init__(self, state, archive, buffers, budget, *, describe=None,
                 available=None, clock=time.time, cache_size=CAPTION_CACHE_SIZE,
                 context_fn=None):
        self.state = state
        self.archive = archive
        self.buffers = buffers
        self.budget = budget
        self.describe = describe
        self.available = available
        self.clock = clock
        self.cache_size = max(1, int(cache_size))
        # context_fn(group_id) -> str：读图时带上群聊上下文（2026-10-20 群主定调，
        # 多模态看图带着对话看才准）。与 describe/available 同风格的依赖注入。
        self.context_fn = context_fn
        self.inflight = {}
        self._closing = False
        self._foreground = {}
        self._targets = {}
        self.cache = self.state.data.setdefault("captions", {})
        self.usage = self.state.data.setdefault("vision_usage", {})
        self._trim_cache()

    def _trim_cache(self):
        while len(self.cache) > self.cache_size:
            self.cache.pop(next(iter(self.cache)))
            self.state.mark_dirty()

    def _claim_slot(self, foreground):
        """只在真正开始新任务时预约一次图片名额；缓存和在途复用不占名额。"""
        if self.budget is None or not self.budget.can_consume():
            return False
        if foreground and self.budget.remaining < 2:
            return False  # reserve one foreground reply opportunity; cache/inflight bypass this gate
        available = (self.available() if self.available is not None
                     else providers.configuration()["vision"]["configured"])
        if not available:
            return False
        now = self.clock()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.usage.get("day") != today:
            self.usage.update(day=today, count=0)
        if self.usage.get("count", 0) >= config.VISION_DAILY_CAP:
            return False
        if (not foreground and now - self.usage.get("last", 0)
                < config.VISION_COOLDOWN_SECONDS):
            return False
        self.usage.update(count=self.usage.get("count", 0) + 1, last=now)
        self.state.mark_dirty()
        return True

    async def _produce(self, image_url):
        if not self._claim_slot(self._foreground.get(image_url, False)):
            return None
        try:
            describe = self.describe or providers.describe_image
            # 上下文按「第一个引用这张图的群」取（QQ 图床 URL 实际不会跨群）；
            # 缓存命中的旧描述不重读，也就不需要上下文。没配 context_fn 时
            # 保持旧调用形态——describe 的旧签名调用方零感知。
            group_id = next((g for g, _mid, _sender
                             in self._targets.get(image_url, ())), None)
            if self.context_fn and group_id:
                caption = await describe(image_url, context_text=self.context_fn(group_id))
            else:
                caption = await describe(image_url)
            if not caption or not str(caption).strip():
                return None
            caption = str(caption).strip()
            self.cache[image_url] = caption
            self._trim_cache()
            self.state.mark_dirty()
            # 关联属于收到的原消息，而非某个等候协程；等候者取消也要保留派生证据。
            for group_id, message_id, sender in self._targets.get(image_url, ()):
                self._attach(group_id, message_id, sender, caption)
            return caption
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("🖼 图片描述失败: %s", str(exc)[:100])
            return None

    def _finished(self, image_url, task):
        if self.inflight.get(image_url) is task:
            self.inflight.pop(image_url, None)
            self._foreground.pop(image_url, None)
            self._targets.pop(image_url, None)

    def _attach(self, group_id, message_id, sender, caption):
        # 派生证据写回唯一原文归档接口；不按相近时间或 [图片] 文本猜归属。
        derived = self.archive.append_caption(group_id, message_id, caption, sender=sender)
        if derived is None:
            return
        marker = f"[图片：{caption}]"
        changed = False
        for entry in self.buffers.get(group_id) or []:
            if (str(entry.get("message_id") or "") != str(message_id)
                    or entry.get("sender") != sender):
                continue
            text = str(entry.get("text") or "")
            if marker not in text:
                entry["text"] = (text.replace("[图片]", marker, 1) if "[图片]" in text
                                 else f"{text} {marker}".strip())
                changed = True
        if changed:
            self.state.data.setdefault("buffers", {})[group_id] = [
                dict(entry) for entry in self.buffers.get(group_id) or []]
            self.state.mark_dirty()

    async def caption_for(self, group_id, message_id, sender, image_url, *, foreground=False):
        """所有读图路径共用同一任务，得到描述后分别按源 ID 关联每条消息。"""
        if self._closing or not config.VISION_ENABLED or not image_url or not message_id:
            return None
        caption = self.cache.get(image_url)
        if caption:
            self._attach(group_id, message_id, sender, caption)
        else:
            self._targets.setdefault(image_url, set()).add((group_id, message_id, sender))
            task = self.inflight.get(image_url)
            if foreground:
                self._foreground[image_url] = True
            if task is None:
                self._foreground.setdefault(image_url, False)
                task = asyncio.create_task(self._produce(image_url))
                self.inflight[image_url] = task
                task.add_done_callback(lambda done: self._finished(image_url, done))
            # 一条消息被取消，不应取消另一路仍在等待的共享读图。
            caption = await asyncio.shield(task)
        return caption

    async def drain(self):
        """Wait for the actual producers, including those protected by shield."""
        tasks = list(self.inflight.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self, *, cancel=False):
        """Stop new work, then finish or cancel producers before providers.close()."""
        self._closing = True
        if cancel:
            for task in list(self.inflight.values()):
                task.cancel()
        await self.drain()


_service = None


def initialize(*, state, archive, buffers, budget, context_fn=None):
    """由 bot 绑定现有 runtime 单例；不创建额外状态文件。"""
    global _service
    _service = CaptionService(state, archive, buffers, budget, context_fn=context_fn)
    return _service


async def caption_for(group_id, message_id, sender, image_url, *, foreground=False):
    if _service is None:
        raise RuntimeError("media.initialize must bind the runtime before reading images")
    return await _service.caption_for(group_id, message_id, sender, image_url,
                                      foreground=foreground)


async def drain():
    if _service is not None:
        await _service.drain()


async def close(*, cancel=False):
    if _service is not None:
        await _service.close(cancel=cancel)
