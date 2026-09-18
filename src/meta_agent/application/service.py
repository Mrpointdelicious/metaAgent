"""
创建日期：2026-09-11
文件功能：统一原生运行入口、去重、截止时间、渐进事件、会话与终态管理。
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from meta_agent.application.composer import ResponseComposer, safe_text
from meta_agent.application.context import RunContext
from meta_agent.config import Settings
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import (
    ConversationState,
    DomainError,
    OutcomeStatus,
    RecordAnchor,
    RunRecord,
    TaskResult,
    fingerprint,
    identifier,
)
from meta_agent.events.stream import EventEmitter
from meta_agent.execution.scheduler import TaskScheduler
from meta_agent.infrastructure.locks import ThreadLockRegistry
from meta_agent.infrastructure.repository import Repository
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.planning.compiler import PlanCompiler
from meta_agent.planning.parser import IntentPlanner

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationRequest:
    query: str
    scope: TrustedScope
    conversation_id: str
    request_id: str


@dataclass(slots=True)
class ApplicationRun:
    record: RunRecord
    emitter: EventEmitter
    task: asyncio.Task | None = None
    reused: bool = False
    context: RunContext | None = None


def aggregate_status(statuses: list[OutcomeStatus]) -> OutcomeStatus:
    if not statuses:
        return "succeeded"
    if len(set(statuses)) == 1:
        return statuses[0]
    if any(s in {"succeeded", "partial"} for s in statuses):
        return "partial"
    for value in (
        "failed",
        "cancelled",
        "clarification",
        "unavailable",
        "unsupported",
        "blocked_dependency",
        "skipped_condition",
    ):
        if value in statuses:
            return value
    return "failed"


class ApplicationService:
    def __init__(
        self,
        *,
        settings: Settings,
        planner: IntentPlanner,
        compiler: PlanCompiler,
        scheduler: TaskScheduler,
        repository: Repository,
        backend: Any,
        tool_limiter: asyncio.Semaphore,
        composer: ResponseComposer | None = None,
    ) -> None:
        self.settings, self.planner, self.compiler = settings, planner, compiler
        self.scheduler, self.repository, self.backend = scheduler, repository, backend
        self.tool_limiter = tool_limiter
        self.composer = composer or ResponseComposer()
        self.request_limiter = asyncio.Semaphore(settings.max_concurrent_requests)
        self.locks = ThreadLockRegistry()
        self.active: dict[str, ApplicationRun] = {}
        self.graph: Any = None
        self.runtime_guard: Any = None

    async def start(self, request: ApplicationRequest) -> ApplicationRun:
        started = time.monotonic()
        if self.runtime_guard:
            await self.runtime_guard()
        scope = request.scope.scope_hash
        digest = fingerprint(
            {
                "query": request.query,
                "scope": scope,
                "conversation_id": request.conversation_id,
                "space": request.scope.space_id,
                "scene_version": request.scope.scene_version,
            }
        )
        async with self.locks.hold("request:" + fingerprint([scope, request.request_id])):
            existing = await self.repository.request_run(scope, request.request_id)
            if existing:
                if existing.request_hash != digest:
                    raise DomainError("idempotency_conflict", "同一请求编号已用于不同内容。")
                if existing.status == "running" and existing.run_id not in self.active:
                    # After restart, do not replay uncertain actions or re-create artifacts.
                    existing.status = "cancelled"
                    for action in existing.action_delivery:
                        existing.action_delivery[action] = "delivery_unknown"
                    if existing.plan:
                        for task in existing.plan.tasks:
                            existing.results.setdefault(
                                task.task_id,
                                TaskResult(
                                    task_id=task.task_id,
                                    status="cancelled",
                                    code="process_interrupted",
                                    message="服务中断，请发起新请求。",
                                    attempts=0,
                                ),
                            )
                        existing.goal_statuses = {g: "cancelled" for g in existing.plan.goal_ids}
                    emitter = EventEmitter(existing, self.repository)
                    await emitter.emit("completed", self.completion(existing))
                return ApplicationRun(
                    existing, EventEmitter(existing, self.repository), reused=True
                )
            record = RunRecord(
                run_id=identifier("run"),
                request_id=request.request_id,
                request_hash=digest,
                scope_key=scope,
                conversation_id=request.conversation_id,
            )
            await self.repository.save_run(record)
            await self.repository.map_request(record)
            emitter = EventEmitter(record, self.repository)
            await emitter.emit("accepted", {"text": "收到，我来处理。"})
            ctx = RunContext(
                query=request.query,
                scope=request.scope,
                record=record,
                memory=ConversationState(),
                settings=self.settings,
                repository=self.repository,
                emitter=emitter,
                backend=self.backend,
                llm_budget=LLMBudget(self.settings.max_llm_calls),
                tool_limiter=self.tool_limiter,
                started=started,
                on_result=self._on_result,
                runtime_guard=self.runtime_guard,
            )
            run = ApplicationRun(record, emitter, context=ctx)
            self.active[record.run_id] = run
            run.task = asyncio.create_task(self._run(ctx), name="metaagent:" + record.run_id)
            return run

    async def execute(self, request: ApplicationRequest) -> ApplicationRun:
        run = await self.start(request)
        if run.task:
            await run.task
        return run

    async def _run(self, ctx: RunContext) -> None:
        thread = ctx.scope.thread_id(ctx.record.conversation_id)
        try:
            async with asyncio.timeout(ctx.remaining):
                async with self.request_limiter, self.locks.hold("conversation:" + thread):
                    ctx.memory = await self.repository.conversation(thread)
                    if self.graph:
                        await self.graph.ainvoke(
                            {"run_id": ctx.record.run_id, "stage": "accepted"},
                            config={"configurable": {"thread_id": ctx.record.run_id}},
                            context=ctx,
                        )
                    else:
                        await self.plan(ctx)
                        await self.execute_plan(ctx)
                    await self._save_memory(ctx, thread)
        except asyncio.CancelledError:
            ctx.interrupted = True
            self._settle(ctx, "cancelled", "request_cancelled", "本次请求已取消。")
        except TimeoutError:
            self._settle(ctx, "failed", "request_timeout", "本次请求已达到执行时限。")
            await ctx.emitter.emit(
                "task_failed",
                {"code": "request_timeout", "text": "本次请求已达到执行时限。", "retryable": False},
            )
        except DomainError as exc:
            self._settle(ctx, exc.outcome, exc.code, exc.message)
            kind = "clarification" if exc.outcome == "clarification" else "task_failed"
            payload = (
                {"text": safe_text(exc.message), "missing_slots": []}
                if kind == "clarification"
                else {"text": safe_text(exc.message), "code": exc.code, "retryable": exc.retryable}
            )
            await ctx.emitter.emit(kind, payload)
        except Exception as exc:
            logger.error(
                "Application failure run_id=%s error_type=%s", ctx.record.run_id, type(exc).__name__
            )
            self._settle(
                ctx, "failed", "application_internal_error", "当前请求处理失败，请稍后重试。"
            )
            await ctx.emitter.emit(
                "task_failed",
                {
                    "code": "application_internal_error",
                    "text": "当前请求处理失败，请稍后重试。",
                    "retryable": False,
                },
            )
        finally:
            try:
                await self._complete(ctx)
            finally:
                await ctx.emitter.finish()
                self.active.pop(ctx.record.run_id, None)

    async def plan(self, ctx: RunContext) -> None:
        await ctx.emitter.emit("progress", {"stage": "planning", "text": "正在确认本轮目标。"})
        decision = await self.planner.parse(ctx.query, ctx.memory, ctx.llm_budget)
        ctx.record.decision = decision
        ctx.goals = {goal.goal_id: goal for goal in decision.goals}
        if decision.decision != "execute":
            ctx.record.status = {
                "respond": "succeeded",
                "clarify": "clarification",
                "unsupported": "unsupported",
            }[decision.decision]
            ctx.record.goal_statuses = {gid: ctx.record.status for gid in ctx.goals}
            if decision.decision == "clarify":
                await ctx.emitter.emit(
                    "clarification",
                    {
                        "text": safe_text(decision.decision_summary or "请进一步明确您的请求。"),
                        "missing_slots": list(
                            dict.fromkeys(slot for g in decision.goals for slot in g.missing_slots)
                        ),
                    },
                )
            else:
                # No tool evidence: only bounded conversational/capability language is allowed.
                text = (
                    "你好，我可以协助查询训练、医生和场景信息。"
                    if decision.decision == "respond"
                    else "该领域或操作暂未支持。"
                )
                await ctx.emitter.emit(
                    "answer_part",
                    {"text": text, "fact_ids": [], "doc_refs": [], "revision": 1, "replaces": None},
                )
            return
        anchor = ctx.memory.current_record
        evidence = (
            await ctx.repository.evidence(ctx.scope.scope_hash, anchor.evidence_id)
            if anchor
            else None
        )
        fresh = evidence is not None and evidence.source_version == anchor.source_version
        ctx.compilation = self.compiler.compile(
            decision, ctx.query, ctx.scope, ctx.memory, ctx.record.request_id, anchor_fresh=fresh
        )
        ctx.record.plan = ctx.compilation.plan
        for gid, (outcome, message) in ctx.compilation.dispositions.items():
            ctx.record.goal_statuses[gid] = outcome
            if outcome == "clarification":
                await ctx.emitter.emit(
                    "clarification",
                    {"text": safe_text(message), "missing_slots": ctx.goals[gid].missing_slots},
                    goal_id=gid,
                )
            else:
                await self.composer.emit_answer(gid, safe_text(message), [], [], ctx)
        await self.repository.save_run(ctx.record)

    async def execute_plan(self, ctx: RunContext) -> None:
        if ctx.record.plan:
            await self.scheduler.execute(ctx.record.plan.tasks, ctx)
            self._update_goal_statuses(ctx)

    async def _on_result(self, task, result, ctx) -> None:
        try:
            await self.composer.on_result(task, result, ctx)
        except DomainError as exc:
            for gid in task.goal_ids:
                ctx.record.goal_statuses[gid] = exc.outcome
            if exc.outcome == "clarification":
                await ctx.emitter.emit(
                    "clarification",
                    {"text": exc.message, "missing_slots": []},
                    task_id=task.task_id,
                )
            else:
                await ctx.emitter.emit(
                    "task_failed",
                    {"text": exc.message, "code": exc.code, "retryable": False},
                    task_id=task.task_id,
                )

    def _update_goal_statuses(self, ctx: RunContext) -> None:
        for gid in ctx.record.plan.goal_ids:
            if gid in ctx.record.goal_statuses:
                continue
            tasks = [t for t in ctx.record.plan.tasks if gid in t.goal_ids]
            internal = {dep for t in tasks for dep in t.depends_on}
            leaves = [t for t in tasks if t.task_id not in internal]
            statuses = [
                ctx.record.results[t.task_id].status
                for t in leaves
                if t.task_id in ctx.record.results
            ]
            outcome = aggregate_status(statuses) if statuses else "unsupported"
            if (
                outcome not in {"succeeded", "partial"}
                and gid in ctx.answer_events
                and any(
                    ctx.record.results[t.task_id].facts
                    for t in tasks
                    if t.task_id in ctx.record.results
                )
            ):
                outcome = "partial"
            ctx.record.goal_statuses[gid] = outcome
        ctx.record.status = aggregate_status(list(ctx.record.goal_statuses.values()))

    def _settle(self, ctx: RunContext, outcome: OutcomeStatus, code: str, message: str) -> None:
        if ctx.record.plan:
            for task in ctx.record.plan.tasks:
                ctx.record.results.setdefault(
                    task.task_id,
                    TaskResult(
                        task_id=task.task_id, status=outcome, code=code, message=message, attempts=0
                    ),
                )
            self._update_goal_statuses(ctx)
        if not ctx.record.goal_statuses or outcome == "cancelled":
            ctx.record.status = outcome

    async def _save_memory(self, ctx: RunContext, thread: str) -> None:
        anchors = {}
        for result in ctx.record.results.values():
            ref = result.outputs.get("session_ref")
            if ref and result.evidence_ids and result.status in {"succeeded", "partial"}:
                anchors[ref] = RecordAnchor(
                    session_ref=ref,
                    evidence_id=result.evidence_ids[0],
                    session_time=result.outputs.get("session_time"),
                    source_version=result.outputs.get("source_version", "unknown"),
                )
            if "history_page" in result.outputs:
                ctx.memory.history_page = result.outputs["history_page"]
                ctx.memory.history_seen = True
        if anchors:
            ctx.memory.current_record = next(iter(anchors.values())) if len(anchors) == 1 else None
        ctx.memory.pending_goals = [
            g
            for gid, g in ctx.goals.items()
            if ctx.record.goal_statuses.get(gid) == "clarification"
        ]
        answers = "\n".join(e.payload["text"] for e in ctx.answer_events.values())
        if not answers:
            answers = "\n".join(
                e.payload["text"]
                for e in ctx.record.events
                if e.type in {"answer_part", "clarification"}
            )
        ctx.memory.turns.append({"user": ctx.query, "assistant": answers})
        await self.repository.save_conversation(thread, ctx.memory)

    @staticmethod
    def completion(record: RunRecord) -> dict:
        return {
            "outcome": record.status,
            "goal_statuses": record.goal_statuses,
            "task_statuses": {tid: result.status for tid, result in record.results.items()},
        }

    async def _complete(self, ctx: RunContext) -> None:
        if ctx.record.status == "running":
            ctx.record.status = aggregate_status(list(ctx.record.goal_statuses.values()))
        ctx.record.metrics.update(
            elapsed_ms=(time.monotonic() - ctx.started) * 1000,
            tool_calls=ctx.tool_calls,
            llm_calls=ctx.llm_budget.calls,
            provider_usage=ctx.llm_budget.usage,
            token_count_method="estimated_unless_provider_usage_present",
        )
        await ctx.emitter.emit("completed", self.completion(ctx.record))
        await self.repository.put(
            "audit",
            "runtime",
            ctx.record.run_id,
            {
                "run_id": ctx.record.run_id,
                "status": ctx.record.status,
                "tool_calls": ctx.tool_calls,
                "llm_calls": ctx.llm_budget.calls,
                "elapsed_ms": ctx.record.metrics["elapsed_ms"],
                "capabilities": [t.capability for t in ctx.record.plan.tasks]
                if ctx.record.plan
                else [],
            },
            self.settings.audit_ttl_seconds,
        )

    async def cancel(self, scope: str, run_id: str) -> RunRecord | None:
        record = await self.repository.run(scope, run_id)
        if record is None:
            return None
        run = self.active.get(run_id)
        if run and run.task:
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
            if run.record.status == "running" and run.context:
                # Cancellation may occur before the coroutine enters its try/finally.
                self._settle(run.context, "cancelled", "request_cancelled", "本次请求已取消。")
                await self._complete(run.context)
                await run.emitter.finish()
                self.active.pop(run_id, None)
            return run.record
        return record

    async def close(self) -> None:
        runs = list(self.active.values())
        await asyncio.gather(
            *(self.cancel(run.record.scope_key, run.record.run_id) for run in runs),
            return_exceptions=True,
        )
