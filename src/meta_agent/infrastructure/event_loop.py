"""
创建日期：2026-09-12
文件功能：为 Windows 本地 PostgreSQL 异步驱动提供兼容的事件循环工厂。
"""

import asyncio


def loop_factory() -> asyncio.AbstractEventLoop:
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    return loop
