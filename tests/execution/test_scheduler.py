"""
创建日期：2026-09-11
文件功能：
验证 TaskScheduler 的 Binding、并行调度、依赖阻断、
partial 继续执行以及 Guard 条件控制。
"""

from __future__ import annotations

import asyncio
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.config import Settings
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import (
    Binding,
    Capability,
    ConversationState,
    DomainError,
    Guard,
    RunRecord,
    TaskResult,
    TaskSpec,
)
from meta_agent.execution.scheduler import TaskScheduler
from meta_agent.orchestration.identity import TrustedScope


class FakeRepository:
    """只记录 save_run 调用，不接真实 Store。"""

    def __init__(self) -> None:
        self.saved = 0

    async def save_run(
        self,
        record: RunRecord,
    ) -> None:
        del record
        self.saved += 1


class FakeDispatcher:
    """
    可编程的领域执行器。

    responses:
        task_id -> TaskResult | DomainError
    """

    def __init__(
        self,
        responses: dict[str, TaskResult | DomainError] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

        self.active = 0
        self.max_active = 0

        # 用于确定性测试并发。
        self.parallel_gate = asyncio.Event()
        self.parallel_mode = False

    async def execute(
        self,
        task: TaskSpec,
        arguments: dict[str, Any],
        ctx: RunContext,
    ) -> TaskResult:
        del ctx

        self.calls.append(
            (
                task.task_id,
                dict(arguments),
            )
        )

        if self.parallel_mode:
            self.active += 1
            self.max_active = max(
                self.max_active,
                self.active,
            )

            # 第二个任务进入后打开 gate，
            # 从而证明两个 task 确实同时处于运行状态。
            if self.active >= 2:
                self.parallel_gate.set()

            await asyncio.wait_for(
                self.parallel_gate.wait(),
                timeout=1,
            )

            self.active -= 1

        response = self.responses.get(task.task_id)

        if isinstance(
            response,
            DomainError,
        ):
            raise response

        if response is not None:
            return response

        return TaskResult(
            task_id=task.task_id,
        )


def make_task(
    task_id: str,
    *,
    capability: Capability = "rehab.overview",
    depends_on: list[str] | None = None,
    bindings: list[Binding] | None = None,
    guards: list[Guard] | None = None,
) -> TaskSpec:

    return TaskSpec(
        task_id=task_id,
        goal_ids=["g1"],
        capability=capability,
        arguments={},
        bindings=bindings or [],
        depends_on=depends_on or [],
        guards=guards or [],
        effect="read",
        priority_class="interactive",
        timeout_ms=5000,
        retry_limit=0,
        idempotency_key=f"test:{task_id}",
    )


def make_context() -> RunContext:
    """创建不访问真实后端的 RunContext。"""

    settings = Settings(
        dry_run=True,
        max_parallel_tasks=4,
        max_tool_calls=16,
        max_llm_calls=4,
    )

    scope = TrustedScope(
        tenant_id="test",
        end_user_id="user-1",
        patient_id="10001",
    )

    record = RunRecord(
        run_id="run-test",
        request_id="request-test",
        request_hash="hash-test",
        scope_key=scope.scope_hash,
        conversation_id="conversation-test",
    )

    return RunContext(
        query="测试",
        scope=scope,
        record=record,
        memory=ConversationState(),
        settings=settings,
        repository=FakeRepository(),  # type: ignore[arg-type]
        emitter=None,  # type: ignore[arg-type]
        backend=None,
        llm_budget=LLMBudget(settings.max_llm_calls),
        tool_limiter=asyncio.Semaphore(settings.max_concurrent_tools),
    )


def test_binding_uses_parent_output() -> None:
    """
    resolve_session 的输出应正确绑定到下游 session。
    """

    async def run() -> None:

        dispatcher = FakeDispatcher(
            responses={
                "t1": TaskResult(
                    task_id="t1",
                    outputs={"session_ref": "session-001"},
                ),
            }
        )

        scheduler = TaskScheduler(
            dispatcher  # type: ignore[arg-type]
        )

        ctx = make_context()

        t1 = make_task(
            "t1",
            capability=("rehab.resolve_session"),
        )

        t2 = make_task(
            "t2",
            capability="rehab.session",
            depends_on=["t1"],
            bindings=[
                Binding(
                    argument="session_ref",
                    source_kind="task_output",
                    source_id="t1",
                    field="session_ref",
                )
            ],
        )

        results = await scheduler.execute(
            [t1, t2],
            ctx,
        )

        assert results["t1"].status == "succeeded"

        assert results["t2"].status == "succeeded"

        t2_calls = [arguments for task_id, arguments in dispatcher.calls if task_id == "t2"]

        assert len(t2_calls) == 1

        assert t2_calls[0]["session_ref"] == "session-001"

    asyncio.run(run())


def test_independent_tasks_run_in_parallel() -> None:
    """
    无依赖的任务应该占用多个并发槽同时运行。
    """

    async def run() -> None:

        dispatcher = FakeDispatcher()
        dispatcher.parallel_mode = True

        scheduler = TaskScheduler(
            dispatcher  # type: ignore[arg-type]
        )

        ctx = make_context()

        t1 = make_task(
            "t1",
            capability="rehab.overview",
        )

        t2 = make_task(
            "t2",
            capability="doctors.search",
        )

        results = await scheduler.execute(
            [t1, t2],
            ctx,
        )

        assert results["t1"].status == "succeeded"

        assert results["t2"].status == "succeeded"

        assert dispatcher.max_active >= 2

    asyncio.run(run())


def test_failed_dependency_blocks_child() -> None:
    """
    failed 的父任务必须阻断其依赖任务。
    """

    async def run() -> None:

        dispatcher = FakeDispatcher(
            responses={
                "t1": DomainError(
                    "synthetic_failure",
                    "模拟失败。",
                    outcome="failed",
                )
            }
        )

        scheduler = TaskScheduler(
            dispatcher  # type: ignore[arg-type]
        )

        ctx = make_context()

        t1 = make_task(
            "t1",
        )

        t2 = make_task(
            "t2",
            capability="rehab.session",
            depends_on=["t1"],
        )

        results = await scheduler.execute(
            [t1, t2],
            ctx,
        )

        assert results["t1"].status == "failed"

        assert results["t2"].status == "blocked_dependency"

        assert results["t2"].code == "dependency_failed"

        assert not any(task_id == "t2" for task_id, _ in dispatcher.calls)

    asyncio.run(run())


def test_partial_dependency_does_not_block_child() -> None:
    """
    partial 代表仍可能存在可用结果，
    不应该默认阻断整个下游。
    """

    async def run() -> None:

        dispatcher = FakeDispatcher(
            responses={
                "t1": TaskResult(
                    task_id="t1",
                    status="partial",
                    outputs={"session_ref": "session-partial"},
                ),
            }
        )

        scheduler = TaskScheduler(
            dispatcher  # type: ignore[arg-type]
        )

        ctx = make_context()

        t1 = make_task(
            "t1",
            capability=("rehab.resolve_session"),
        )

        t2 = make_task(
            "t2",
            capability="rehab.session",
            depends_on=["t1"],
        )

        results = await scheduler.execute(
            [t1, t2],
            ctx,
        )

        assert results["t1"].status == "partial"

        assert results["t2"].status == "succeeded"

        assert any(task_id == "t2" for task_id, _ in dispatcher.calls)

    asyncio.run(run())


def test_guard_skip_prevents_dispatch() -> None:
    """
    Guard 条件为 false 且 on_false=skip 时，
    子任务必须 skipped_condition，
    且不能进入 DomainDispatcher。
    """

    async def run() -> None:

        dispatcher = FakeDispatcher(
            responses={
                "t1": TaskResult(
                    task_id="t1",
                    outputs={
                        "has_results": False,
                        "no_results": True,
                    },
                ),
            }
        )

        scheduler = TaskScheduler(
            dispatcher  # type: ignore[arg-type]
        )

        ctx = make_context()

        t1 = make_task(
            "t1",
            capability="doctors.search",
        )

        t2 = make_task(
            "t2",
            capability="scene.dispatch",
            depends_on=["t1"],
            guards=[
                Guard(
                    source_task_id="t1",
                    predicate="has_results",
                    on_false="skip",
                )
            ],
        )

        results = await scheduler.execute(
            [t1, t2],
            ctx,
        )

        assert results["t1"].status == "succeeded"

        assert results["t2"].status == "skipped_condition"

        assert results["t2"].code == "guard_not_satisfied"

        assert not any(task_id == "t2" for task_id, _ in dispatcher.calls)

    asyncio.run(run())
