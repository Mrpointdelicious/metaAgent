"""
创建日期：2026-09-12
文件功能：按动作、交互查询、制品顺序分配全局工具并发槽。
"""

import asyncio
import heapq
import itertools
from contextlib import asynccontextmanager


class PriorityLimiter:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.active = 0
        self.waiters = []
        self.counter = itertools.count()
        self.condition = asyncio.Condition()

    @asynccontextmanager
    async def hold(self, priority: int):
        ticket = (priority, next(self.counter))
        async with self.condition:
            heapq.heappush(self.waiters, ticket)
            try:
                await self.condition.wait_for(
                    lambda: self.active < self.maximum and self.waiters[0] == ticket
                )
                heapq.heappop(self.waiters)
                self.active += 1
                self.condition.notify_all()
            except BaseException:
                self.waiters.remove(ticket)
                heapq.heapify(self.waiters)
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()
