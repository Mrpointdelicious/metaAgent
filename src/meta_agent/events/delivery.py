"""
创建日期：2026-10-09
文件功能：按序交付已提交事件，协调协议转换、传输标记与断线取消。
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from meta_agent.events.native import OutputAdapter
from meta_agent.events.stream import EventEmitter


class EventDelivery:
    def __init__(
        self,
        emitter: EventEmitter,
        adapter: OutputAdapter,
        cancel: Callable[[], Awaitable[object]],
    ) -> None:
        self.emitter, self.adapter, self.cancel = emitter, adapter, cancel

    async def stream(self) -> AsyncIterator[str]:
        exhausted = False
        cursor = 0
        try:
            while (event := await self.emitter.next_event(cursor)) is not None:
                if event.type == "action_ready" and not self.adapter.supports_actions:
                    raise RuntimeError("Output adapter does not support actions")
                frames = self.adapter.encode_event(event)
                if event.type == "action_ready" and not frames:
                    raise RuntimeError("Output adapter discarded an action")
                for frame in frames:
                    yield frame
                if frames:
                    await self.emitter.mark_dispatched(event)
                cursor = event.seq
            exhausted = True
        finally:
            if not exhausted:

                async def terminate():
                    try:
                        await self.emitter.disconnect()
                    finally:
                        await self.cancel()

                await asyncio.shield(terminate())
