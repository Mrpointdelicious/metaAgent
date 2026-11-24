"""
创建日期：2026-11-17
文件功能：在独立 PostgreSQL 验证偏好表迁移、约束、读写和重连持久化。
"""

import asyncio
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from meta_agent.application.conversations import owner_key
from meta_agent.infrastructure.container import create_container
from meta_agent.infrastructure.migrate import migrate
from meta_agent.memory.postgres import PostgresPreferenceRepository
from meta_agent.memory.schemas import MemoryArgs, PreferenceError
from tests.helpers.identity import patient_scope
from tests.helpers.settings import AppTestSettings

DSN = os.environ.get("META_AGENT_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="需要独立的 META_AGENT_TEST_POSTGRES_DSN")


def test_postgres_preference_schema_and_roundtrip():
    assert urlsplit(DSN).path == "/metaagent_test", "只允许明确的 metaagent_test 数据库"

    async def check():
        from psycopg.errors import CheckViolation, UniqueViolation
        from psycopg_pool import AsyncConnectionPool

        scope = patient_scope("memory-test", str(uuid4()))
        owner = owner_key(scope)
        args = MemoryArgs(content={"key": "answer_length", "value": "short"})
        settings = AppTestSettings(
            app_env="test",
            persistence_backend="postgres",
            postgres_dsn=DSN,
            patient_memory_enabled=True,
            auto_setup_persistence=False,
        )
        await migrate(settings)
        async with AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False) as pool:
            repo = PostgresPreferenceRepository(pool)
            await repo.setup()  # Migration is repeatable.
            await repo.ready()
            try:
                first = await repo.apply(owner, args, uuid4())
                before = (await repo.snapshot(owner)).items[0]
                repeated = await asyncio.gather(
                    *[repo.apply(owner, args, uuid4()) for _ in range(5)]
                )
                assert all(r.id == first.id for r in repeated)
                assert (await repo.snapshot(owner)).items == (before,)
                await repo.apply(
                    owner,
                    MemoryArgs(
                        action="update",
                        id=first.id,
                        content={"key": "answer_length", "value": "detailed"},
                    ),
                    uuid4(),
                )
                after = (await repo.snapshot(owner)).items[0]
                assert after.id == before.id and after.content.value == "detailed"
                assert (
                    after.created_at == before.created_at and after.updated_at >= before.updated_at
                )
                with pytest.raises(PreferenceError, match="not_found"):
                    await repo.apply(
                        "b" * 64,
                        MemoryArgs(
                            action="update",
                            id=first.id,
                            content=args.content,
                        ),
                        uuid4(),
                    )
                with pytest.raises(PreferenceError, match="key_mismatch"):
                    await repo.apply(
                        owner,
                        MemoryArgs(
                            action="update",
                            id=first.id,
                            content={"key": "language", "value": "en"},
                        ),
                        uuid4(),
                    )
                for key, value in [
                    ("diagnosis", "synthetic-disease"),
                    ("tone", "synthetic-disease"),
                    ("language", "short"),
                ]:
                    with pytest.raises(CheckViolation):
                        async with pool.connection() as conn, conn.transaction():
                            await conn.execute(
                                "INSERT INTO public.patient_preferences "
                                "(owner_key, memory_id, preference_key, preference_value) "
                                "VALUES (%s,%s,%s,%s)",
                                (owner, uuid4(), key, value),
                            )
                with pytest.raises(UniqueViolation):
                    async with pool.connection() as conn, conn.transaction():
                        await conn.execute(
                            "INSERT INTO public.patient_preferences "
                            "(owner_key, memory_id, preference_key, preference_value) "
                            "VALUES (%s,%s,%s,%s)",
                            (owner, uuid4(), "answer_length", "short"),
                        )
                # A separate application pool sees persisted preferences; existing TTL cleanup
                # does not own or remove this table.
                container = await create_container(settings)
                try:
                    await container.repository.put("cache", "memory-test", "expired", {}, -1)
                    await container.repository.cleanup()
                    snapshot = await container.patient_memory.repository.snapshot(owner)
                    assert snapshot.items == (after,)
                    assert not (await container.patient_memory.repository.snapshot("b" * 64)).items
                finally:
                    await container.close()
            finally:
                # Test fixture cleanup only, not an application deletion feature.
                async with pool.connection() as conn:
                    await conn.execute(
                        "DELETE FROM public.patient_preferences WHERE owner_key = %s", (owner,)
                    )

    if sys.platform == "win32":
        asyncio.run(check(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(check())
