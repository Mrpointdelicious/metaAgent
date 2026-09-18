"""
创建日期：2026-09-08
文件功能：保存不进入Checkpoint的运行依赖、预算和可信会话上下文。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from meta_agent.config import Settings
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, DomainError, Goal, RunRecord
from meta_agent.events.stream import EventEmitter
from meta_agent.infrastructure.limiter import PriorityLimiter
from meta_agent.infrastructure.repository import Repository
from meta_agent.orchestration.identity import TrustedScope


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
    compilation: Any = None
    goals: dict[str, Goal] = field(default_factory=dict)
    answer_fingerprints: dict[str, str] = field(default_factory=dict)
    answer_events: dict[str, Any] = field(default_factory=dict)
    artifact_refs: set[str] = field(default_factory=set)
    interrupted: bool = False
    on_result: Any = None
    runtime_guard: Any = None

    @property
    def remaining(self) -> float:
        return max(0, self.settings.request_timeout_seconds - (time.monotonic() - self.started))

    async def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.runtime_guard:
            await self.runtime_guard()
        if self.tool_calls >= self.settings.max_tool_calls:
            raise DomainError("tool_budget", "本轮工具调用预算已用完。", outcome="unavailable")
        self.tool_calls += 1
        priority = (
            0 if endpoint == "navigate_scene" else 2 if endpoint.startswith("generate_") else 1
        )
        lease = (
            self.tool_limiter.hold(priority)
            if isinstance(self.tool_limiter, PriorityLimiter)
            else self.tool_limiter
        )
        async with lease:
            return await self.backend.post(endpoint, payload)
