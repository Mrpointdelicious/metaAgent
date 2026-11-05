"""
创建日期：2026-10-30
文件功能：创建隔离的业务集成测试上下文，不调用真实服务。
"""

from meta_agent.application.context import RunContext
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, RunRecord, identifier
from meta_agent.events.stream import EventEmitter
from meta_agent.orchestration.identity import TrustedScope


def context(container, query="你好", *, scope=None):
    scope = scope or TrustedScope("tenant", "actor", space_id="space", scene_version=7)
    record = RunRecord(
        run_id=identifier("run"),
        request_id=identifier("request"),
        request_hash="synthetic",
        scope_key=scope.scope_hash,
        conversation_id="c1",
    )
    return RunContext(
        query=query,
        scope=scope,
        record=record,
        memory=ConversationState(),
        settings=container.settings,
        repository=container.repository,
        emitter=EventEmitter(record, container.repository),
        backend=container.application.backend,
        llm_budget=LLMBudget(container.settings.agent_max_model_calls),
        tool_limiter=container.application.tool_limiter,
    )
