"""
创建日期：2026-09-12
文件功能：在部署前显式初始化 LangGraph PostgreSQL Store 和 Checkpoint 表。
"""

import asyncio
import sys

from meta_agent.config import Settings


async def migrate(settings: Settings) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from psycopg.conninfo import make_conninfo

    if not settings.postgres_dsn:
        raise RuntimeError("迁移需要 META_AGENT__POSTGRES_DSN")
    dsn = make_conninfo(settings.postgres_dsn, connect_timeout=5)
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    async with AsyncPostgresStore.from_conn_string(dsn) as store:
        await store.setup()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(migrate(Settings()), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(migrate(Settings()))
