"""
创建日期：2026-09-12
文件功能：在显式指定的独立测试数据库验证迁移、恢复、单实例锁与物理清理。
"""

import asyncio
import os
import sys
from urllib.parse import urlsplit

import pytest

from meta_agent.infrastructure.container import create_container
from meta_agent.infrastructure.migrate import migrate
from tests.helpers.runtime import Backend, request, scene_response
from tests.helpers.settings import AppTestSettings

DSN = os.environ.get("META_AGENT_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="需要独立的 META_AGENT_TEST_POSTGRES_DSN")


def test_postgres_migration_restart_dedup_lock_and_cleanup():
    assert urlsplit(DSN).path == "/metaagent_test", "只允许明确的 metaagent_test 数据库"

    async def check():
        settings = AppTestSettings(
            app_env="test",
            dry_run=True,
            persistence_backend="postgres",
            postgres_dsn=DSN,
            auto_setup_persistence=False,
            scene_actions_enabled=True,
            planner_mode="deterministic",
        )
        await migrate(settings)
        c = await create_container(settings)
        req = request("打开面板", "pg-restart-" + str(__import__("time").time_ns()))
        try:
            backend = Backend()
            backend.overrides["navigate_scene"] = scene_response
            c.application.backend = backend
            run = await c.application.execute(req)
            assert run.record.status == "succeeded"
            assert len(run.record.action_delivery) == 1
            with pytest.raises(RuntimeError, match="已有运行实例"):
                await create_container(settings)
            # Simulate interruption after an action was durably registered.
            run.record.status = "running"
            run.record.events = [e for e in run.record.events if e.type != "completed"]
            await c.repository.save_run(run.record)
        finally:
            await c.close()
        c = await create_container(settings)
        try:
            backend = Backend()
            c.application.backend = backend
            resumed = await c.application.execute(req)
            assert resumed.reused and not backend.calls
            assert resumed.record.status == "cancelled"
            assert set(resumed.record.action_delivery.values()) == {"delivery_unknown"}
            assert resumed.emitter.queue.empty()
            assert await c.repository.run("wrong", resumed.record.run_id) is None
            await c.repository.put("cache", "test-postgres", "expired", {"demo": True}, -1)
            assert await c.repository.cleanup() >= 1
            assert (
                await c.repository.store.aget(
                    c.repository.namespace("cache", "test-postgres"), "expired"
                )
                is None
            )
            await c.repository.put(
                "runs",
                req.scope.scope_hash,
                resumed.record.run_id,
                resumed.record.model_dump(mode="json"),
                -1,
            )
            await c.repository.cleanup()
            snapshot = await c.graph.aget_state(
                {"configurable": {"thread_id": resumed.record.run_id}}
            )
            assert snapshot.values == {}
        finally:
            await c.close()

    if sys.platform == "win32":
        asyncio.run(check(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(check())
