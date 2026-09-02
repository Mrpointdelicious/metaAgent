"""
创建日期：2026-08-29
文件功能：提供按 LangGraph thread_id 串行、不同会话并行的进程内异步锁。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class ThreadLockRegistry:
    """避免同一会话的并发请求相互覆盖 Checkpoint。"""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncIterator[None]:
        async with self._guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
        async with lock:
            yield
