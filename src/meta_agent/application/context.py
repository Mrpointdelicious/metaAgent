"""
创建日期：2026-09-08
文件功能：保存不进入Checkpoint的运行依赖、预算和可信会话上下文。
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from meta_agent.config import Settings
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, DomainError, Goal, RunRecord
from meta_agent.domains.patient_identity import PatientBrief
from meta_agent.events.stream import EventEmitter
from meta_agent.infrastructure.limiter import PriorityLimiter
from meta_agent.infrastructure.repository import Repository
from meta_agent.medical_tools.contracts import ToolContext
from meta_agent.medical_tools.gateway import MedicalToolGateway
from meta_agent.medical_tools.registry import require_enabled
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.roles import ROLE_AGENTS, RoleAgentProfile

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    query: str
    scope: TrustedScope
    record: RunRecord
    memory: ConversationState
    settings: Settings
    repository: Repository
    emitter: EventEmitter
    backend: Any
    llm_budget: LLMBudget
    tool_limiter: asyncio.Semaphore | PriorityLimiter
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    scene_invalidated: bool = False
    patient_brief: PatientBrief | None = None
    compilation: Any = None
    goals: dict[str, Goal] = field(default_factory=dict)
    answer_fingerprints: dict[str, str] = field(default_factory=dict)
    answer_events: dict[str, Any] = field(default_factory=dict)
    artifact_refs: set[tuple[str, str]] = field(default_factory=set)
    interrupted: bool = False
    on_result: Any = None
    runtime_guard: Any = None
    # 分支流程可在调用 agent 前设置，用于对最终回答做后处理（如剥离 reasoning 标记）。
    # 签名：processor(answer: str, ctx: RunContext) -> str | Awaitable[str]。
    answer_post_processor: Any = None
    # 子代理（如基础问答与指令）在本轮使用的系统提示词覆盖；None 时用 settings 或默认。
    system_prompt: str | None = None
    # 请求级诊断快照（仅日志用，不进入 checkpoint/证据/审计）：原始 query 与脱敏输入。
    # 由 service.start() 填充，_complete() 在请求结束时打一条 request_summary。
    diagnostic_query: str = ""
    diagnostic_inputs: dict[str, Any] = field(default_factory=dict)
    # 工具网关缓存（按 backend 实例复用）；backend 被替换时自动重建，见 gateway 属性。
    _gateway: Any = field(default=None, init=False, repr=False, compare=False)

    @property
    def gateway(self) -> MedicalToolGateway:
        """工具统一入口：注册表校验 + 提供方路由（本地中台场景/医生 或 .NET）。"""
        backend = self.backend
        cached = self._gateway
        if cached is None or cached.backend is not backend:
            cached = MedicalToolGateway.wrap(backend, self.settings)
            self._gateway = cached
        return cached

    # 认证后的接入方身份（ClientIdentity）；决定工具可见性等按客户端差异，None 表示无客户端约束。
    client: Any = None
    # Runtime-only preference access; never serialized into conversation history or evidence.
    preference_owner: str | None = None
    preference_unavailable: bool = False
    # Whitelisted arguments for the authenticated fixed flow, held for this run only.
    flow_inputs: dict[str, Any] = field(default_factory=dict, repr=False)
    # Fallback clinical lookup inputs used only when disable_capability_policy=True.
    fallback_business_inputs: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def profile(self) -> RoleAgentProfile:
        return ROLE_AGENTS.for_role(self.scope.role)

    @property
    def business_inputs(self):
        grant = self.scope.resource_grant
        if grant and grant.valid_for(self.scope):
            return {**self.flow_inputs, **grant.lookup_inputs()}
        if self.settings.disable_capability_policy:
            return {**self.fallback_business_inputs, **self.flow_inputs}
        return {}

    @property
    def remaining(self) -> float:
        return max(0, self.settings.request_timeout_seconds - (time.monotonic() - self.started))

    async def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        require_enabled(endpoint, self.settings)
        priority = (
            0 if endpoint == "navigate_scene" else 2 if endpoint.startswith("generate_") else 1
        )
        context = ToolContext(
            scope=self.scope,
            request_id=self.record.request_id,
            conversation_id=self.record.conversation_id,
            excluded_tools=self.client.excluded_tools if self.client else frozenset(),
        )
        async with self.tool_slot(priority):
            result = await self.gateway.execute(endpoint, payload, context)
        return result.domain_payload()

    @asynccontextmanager
    async def tool_slot(self, priority: int = 1):
        """Share admission, call budget and concurrency with external knowledge services."""
        if self.runtime_guard:
            await self.runtime_guard()
        if self.tool_calls >= self.settings.max_tool_calls:
            raise DomainError("tool_budget", "本轮工具调用预算已用完。", outcome="unavailable")
        self.tool_calls += 1
        lease = (
            self.tool_limiter.hold(priority)
            if isinstance(self.tool_limiter, PriorityLimiter)
            else self.tool_limiter
        )
        async with lease:
            yield
