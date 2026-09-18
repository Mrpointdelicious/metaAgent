"""
创建日期：2026-09-03
文件功能：装配原生执行链、数据库单实例租约和定时物理清理。
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from meta_agent.application.composer import ResponseComposer
from meta_agent.application.service import ApplicationService
from meta_agent.config import Settings
from meta_agent.contracts import DomainError
from meta_agent.execution.dispatcher import DomainDispatcher
from meta_agent.execution.scheduler import TaskScheduler
from meta_agent.graph.runtime import build_runtime_graph
from meta_agent.infrastructure.limiter import PriorityLimiter
from meta_agent.infrastructure.repository import Repository
from meta_agent.llm.models import create_planner_model
from meta_agent.planning.compiler import PlanCompiler
from meta_agent.planning.parser import ConservativePlanner, IntentPlanner, StructuredIntentPlanner
from meta_agent.tools.ai_webapi import AIWebApiClient

logger = logging.getLogger(__name__)
INSTANCE_LOCK = 730914081  # Stable per database: v1 does not provide cross-process run leases.


@dataclass(slots=True)
class ServiceContainer:
    settings: Settings
    graph: Any
    repository: Repository
    application: ApplicationService
    ai_webapi_client: AIWebApiClient
    exit_stack: AsyncExitStack
    persistence_status: str
    cleanup_task: asyncio.Task | None = None
    lease_connection: Any = None

    async def close(self) -> None:
        await self.application.close()
        if self.cleanup_task:
            self.cleanup_task.cancel()
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
        await self.exit_stack.aclose()

    async def ready(self) -> bool:
        if self.lease_connection:
            await self.lease_connection.execute("SELECT 1")
        return await self.repository.ready()


def create_task_planner(settings: Settings) -> IntentPlanner:
    if settings.planner_mode == "deterministic":
        return ConservativePlanner()
    return StructuredIntentPlanner(create_planner_model(settings), settings)


async def cleanup_loop(container: ServiceContainer) -> None:
    while True:
        await asyncio.sleep(container.settings.cleanup_interval_seconds)
        try:
            await container.repository.cleanup()
        except Exception as exc:
            logger.error("TTL cleanup failed error_type=%s", type(exc).__name__)


async def create_container(settings: Settings) -> ServiceContainer:
    issues = settings.production_issues()
    if issues:
        raise RuntimeError("；".join(issues))
    stack = AsyncExitStack()
    await stack.__aenter__()
    lease = None
    try:
        if settings.persistence_backend == "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.store.postgres.aio import AsyncPostgresStore
            from psycopg import AsyncConnection
            from psycopg.conninfo import make_conninfo

            if not settings.postgres_dsn:
                raise RuntimeError("PostgreSQL 持久化缺少 META_AGENT__POSTGRES_DSN")
            dsn = make_conninfo(settings.postgres_dsn, connect_timeout=5)
            lease = await stack.enter_async_context(
                await AsyncConnection.connect(dsn, autocommit=True)
            )
            cursor = await lease.execute("SELECT pg_try_advisory_lock(%s)", (INSTANCE_LOCK,))
            if not (await cursor.fetchone())[0]:
                raise RuntimeError("v1数据库已有运行实例；尚不支持多worker或多副本")
            checkpointer = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(dsn))
            store = await stack.enter_async_context(AsyncPostgresStore.from_conn_string(dsn))
            if settings.auto_setup_persistence:
                await checkpointer.setup()
                await store.setup()
            persistence = "postgres"
        else:
            checkpointer, store = InMemorySaver(), InMemoryStore()
            persistence = "memory"
        client = AIWebApiClient(settings)
        stack.push_async_callback(client.close)
        repository = Repository(store, settings, checkpointer)
        await repository.ready()
        await checkpointer.aget_tuple({"configurable": {"thread_id": "metaagent-startup-probe"}})
        model = None
        if settings.answer_mode == "llm_select":
            model = create_planner_model(
                settings.model_copy(
                    update={
                        "planner_model": settings.answer_model,
                        "planner_timeout_seconds": settings.answer_timeout_seconds,
                        "planner_max_retries": 0,
                    }
                )
            )
        application = ApplicationService(
            settings=settings,
            planner=create_task_planner(settings),
            compiler=PlanCompiler(settings),
            scheduler=TaskScheduler(DomainDispatcher()),
            repository=repository,
            backend=client,
            tool_limiter=PriorityLimiter(settings.max_concurrent_tools),
            composer=ResponseComposer(model),
        )
        graph = build_runtime_graph(application, checkpointer, store)
        application.graph = graph
        container = ServiceContainer(
            settings,
            graph,
            repository,
            application,
            client,
            stack,
            persistence,
            lease_connection=lease,
        )
        if lease:

            async def guard():
                try:
                    async with asyncio.timeout(2):
                        await lease.execute("SELECT 1")
                except Exception as exc:
                    raise DomainError(
                        "instance_lease_lost", "运行实例租约不可用，已停止新操作。"
                    ) from exc

            application.runtime_guard = guard
        await repository.cleanup()
        container.cleanup_task = asyncio.create_task(
            cleanup_loop(container), name="metaagent:ttl-cleanup"
        )
        return container
    except BaseException:
        await stack.aclose()
        raise
