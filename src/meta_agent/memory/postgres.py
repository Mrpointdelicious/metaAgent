"""
创建日期：2026-11-17
文件功能：用独立 PostgreSQL 表持久化非医疗偏好，仅实现读取和新增更新。
"""

from importlib.resources import files
from uuid import UUID

from meta_agent.memory.repository import validate_owner
from meta_agent.memory.schemas import (
    MemoryArgs,
    MemoryChange,
    Preference,
    PreferenceError,
    PreferenceRecord,
    PreferenceSnapshot,
)


class PostgresPreferenceRepository:
    def __init__(self, pool):
        self.pool = pool

    async def setup(self):
        sql = files("meta_agent.memory").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(sql)

    async def ready(self):
        async with self.pool.connection() as conn:
            await conn.execute("SELECT memory_id FROM public.patient_preferences LIMIT 0")

    async def snapshot(self, owner: str) -> PreferenceSnapshot:
        validate_owner(owner)
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT memory_id, preference_key, preference_value, created_at, updated_at "
                "FROM public.patient_preferences WHERE owner_key = %s ORDER BY preference_key",
                (owner,),
            )
            items = tuple(
                PreferenceRecord(row[0], Preference(key=row[1], value=row[2]), row[3], row[4])
                for row in await cursor.fetchall()
            )
            return PreferenceSnapshot(items)

    async def apply(self, owner: str, args: MemoryArgs, generated_id: UUID) -> MemoryChange:
        validate_owner(owner)
        args = MemoryArgs.model_validate(args.model_dump())
        async with self.pool.connection() as conn, conn.transaction():
            if args.action == "create":
                # One atomic upsert per (owner, preference key); retain the original UUID.
                cursor = await conn.execute(
                    "INSERT INTO public.patient_preferences AS current "
                    "(owner_key, memory_id, preference_key, preference_value) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (owner_key, preference_key) DO UPDATE "
                    "SET preference_value = EXCLUDED.preference_value, "
                    "updated_at = CASE WHEN current.preference_value = EXCLUDED.preference_value "
                    "THEN current.updated_at ELSE clock_timestamp() END "
                    "RETURNING memory_id",
                    (owner, generated_id, args.content.key, args.content.value),
                )
                memory_id = (await cursor.fetchone())[0]
                return MemoryChange("saved", memory_id)
            cursor = await conn.execute(
                "UPDATE public.patient_preferences SET preference_value = %s, "
                "updated_at = CASE WHEN preference_value = %s THEN updated_at "
                "ELSE clock_timestamp() END "
                "WHERE owner_key = %s AND memory_id = %s AND preference_key = %s "
                "RETURNING memory_id",
                (args.content.value, args.content.value, owner, args.id, args.content.key),
            )
            row = await cursor.fetchone()
            if row is None:
                raise PreferenceError("memory_not_found_or_key_mismatch")
            return MemoryChange("saved", row[0])
