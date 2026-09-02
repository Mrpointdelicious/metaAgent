"""
创建日期：2026-08-29
文件功能：创建并管理 Checkpoint、Store、HTTP 客户端和主图的生命周期。
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from meta_agent.config import Settings
from meta_agent.context.compiler import ContextCompiler
from meta_agent.evidence.store import EvidenceRepository
from meta_agent.graph.builder import build_agent_graph
from meta_agent.infrastructure.locks import ThreadLockRegistry
from meta_agent.orchestration.planner import DeterministicTaskPlanner
from meta_agent.orchestration.validator import PlanValidator
from meta_agent.tools.ai_webapi import AIWebApiClient
from meta_agent.validation.claims import ClaimValidator
from meta_agent.workflows.irego import IReGoWorkflow


@dataclass(slots=True)
class ServiceContainer:
    """保存单个进程共享、线程安全的服务依赖。"""

    settings: Settings
    graph: Any
    evidence_repository: EvidenceRepository
    ai_webapi_client: AIWebApiClient
    exit_stack: AsyncExitStack
    persistence_status: str
    request_limiter: asyncio.Semaphore
    thread_locks: ThreadLockRegistry

    async def close(self) -> None:
        """按反向顺序释放数据库和网络资源。"""
        await self.exit_stack.aclose()


async def create_container(settings: Settings) -> ServiceContainer:
    """根据环境选择内存或 PostgreSQL 持久化并装配应用依赖。"""
    exit_stack = AsyncExitStack()
    await exit_stack.__aenter__()

    if settings.persistence_backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore

        if not settings.postgres_dsn:
            await exit_stack.aclose()
            raise RuntimeError("PostgreSQL 持久化缺少 META_AGENT__POSTGRES_DSN")
        checkpointer = await exit_stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.postgres_dsn)
        )
        store = await exit_stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(settings.postgres_dsn)
        )
        if settings.auto_setup_persistence:
            await checkpointer.setup()
            await store.setup()
        persistence_status = "postgres"
    else:
        checkpointer = InMemorySaver()
        store = InMemoryStore()
        persistence_status = "memory"

    client = AIWebApiClient(settings)
    exit_stack.push_async_callback(client.close)
    evidence_repository = EvidenceRepository(store, settings.evidence_ttl_seconds)
    workflow = IReGoWorkflow(client, settings)
    graph = build_agent_graph(
        checkpointer=checkpointer,
        planner=DeterministicTaskPlanner(),
        plan_validator=PlanValidator(),
        workflow=workflow,
        evidence_repository=evidence_repository,
        context_compiler=ContextCompiler(),
        claim_validator=ClaimValidator(),
    )
    return ServiceContainer(
        settings=settings,
        graph=graph,
        evidence_repository=evidence_repository,
        ai_webapi_client=client,
        exit_stack=exit_stack,
        persistence_status=persistence_status,
        request_limiter=asyncio.Semaphore(settings.max_concurrent_requests),
        thread_locks=ThreadLockRegistry(),
    )
