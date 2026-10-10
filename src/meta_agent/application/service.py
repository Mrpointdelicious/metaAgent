"""
创建日期：2026-09-11
文件功能：统一原生运行入口、去重、截止时间、渐进事件、会话与终态管理。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from meta_agent.application.composer import ResponseComposer, safe_text
from meta_agent.application.context import RunContext
from meta_agent.application.frontend_context import conversational_only, prepare_frontend_context
from meta_agent.config import Settings
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import (
    ClarificationPayload,
    ConversationState,
    DomainError,
    FailurePayload,
    OutcomeStatus,
    RecordAnchor,
    RunRecord,
    TaskResult,
    fingerprint,
    identifier,
    utcnow,
)
from meta_agent.domains.patient_identity import brief_from_context_response
from meta_agent.events.publisher import AnswerContent, RunEventPublisher, RunOutcome
from meta_agent.events.stream import EventEmitter, EventPersistenceError
from meta_agent.execution.scheduler import TaskScheduler
from meta_agent.infrastructure.locks import ThreadLockRegistry
from meta_agent.infrastructure.repository import Repository
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.planning.compiler import PlanCompiler
from meta_agent.planning.parser import IntentPlanner
from meta_agent.prompts.service import PromptService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationRequest:
    query: str
    scope: TrustedScope
    conversation_id: str
    request_id: str
    context: dict[str, Any] = field(default_factory=dict)
    owner_key: str | None = None
    public_request_hash: str | None = None


@dataclass(slots=True)
class ApplicationRun:
    record: RunRecord
    emitter: EventEmitter
    task: asyncio.Task | None = None
    reused: bool = False
    context: RunContext | None = None
    admitted: bool = False


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
        agent_runtime: Any = None,
        prompts: PromptService | None = None,
    ) -> None:
        self.settings, self.planner, self.compiler = settings, planner, compiler
        self.scheduler, self.repository, self.backend = scheduler, repository, backend
        self.tool_limiter = tool_limiter
        self.composer = composer or ResponseComposer()
        self.agent_runtime = agent_runtime
        self.prompts = prompts or PromptService()
        self.prompts.bind(settings.prompt_bundle)
        self.request_limiter = asyncio.Semaphore(settings.max_concurrent_requests)
        self.admission_limiter = asyncio.Semaphore(settings.max_concurrent_requests)
        self.locks = ThreadLockRegistry()
        self.active: dict[str, ApplicationRun] = {}
        self.graph: Any = None
        self.runtime_guard: Any = None

    async def start(self, request: ApplicationRequest) -> ApplicationRun:
        started = time.monotonic()
        if self.runtime_guard:
            await self.runtime_guard()
        scope = request.scope.scope_hash
        digest = request.public_request_hash or fingerprint(
            {
                "query": request.query,
                "scope": scope,
                "conversation_id": request.conversation_id,
                "space": request.scope.space_id,
                "scene_version": request.scope.scene_version,
                **({"context": request.context} if request.owner_key else {}),
            }
        )
        request_namespace = request.owner_key or scope
        async with self.locks.hold(
            "request:" + fingerprint([request_namespace, request.request_id])
        ):
            existing = await self.repository.request_run(request_namespace, request.request_id)
            if existing:
                if existing.request_hash != digest:
                    raise DomainError("idempotency_conflict", "同一请求编号已用于不同内容。")
                if request.owner_key:
                    restored = await self.owned_run(request.owner_key, existing.run_id)
                    if restored is None:
                        raise DomainError("run_expired", "运行已过期，请使用新的请求编号。")
                    return ApplicationRun(restored.record, restored.emitter, reused=True)
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
                    await RunEventPublisher(emitter).completed(self.completion(existing))
                return ApplicationRun(
                    existing, EventEmitter(existing, self.repository, streamable=False), reused=True
                )
            return await self._start_new(request, digest, started)

    async def _start_new(self, request: ApplicationRequest, digest: str, started: float):
        admitted = request.owner_key is not None
        if admitted:
            if self.admission_limiter.locked():
                raise DomainError("service_busy", "服务繁忙，请稍后重试。", retryable=True)
            await self.admission_limiter.acquire()
        try:
            run = await self._build_run(request, digest, started)
        except BaseException:
            if admitted:
                self.admission_limiter.release()
            raise
        run.admitted = admitted
        return run

    async def _build_run(self, request: ApplicationRequest, digest: str, started: float):
        scope = request.scope.scope_hash
        request_namespace = request.owner_key or scope
        prompt_binding = self.prompts.bind(self.settings.prompt_bundle)
        record = RunRecord(
            run_id=identifier("run"),
            request_id=request.request_id,
            request_hash=digest,
            scope_key=scope,
            conversation_id=request.conversation_id,
        )
        await self.repository.save_run(record)
        if request.owner_key:
            await self.repository.put(
                "agent_runs",
                request.owner_key,
                record.run_id,
                {"scope_key": scope},
                self.settings.run_ttl_seconds,
            )
        await self.repository.map_request(record, namespace=request_namespace)
        emitter = EventEmitter(record, self.repository)
        ctx = RunContext(
            query=request.query,
            scope=request.scope,
            record=record,
            memory=ConversationState(),
            settings=self.settings,
            repository=self.repository,
            emitter=emitter,
            backend=self.backend,
            llm_budget=LLMBudget(
                self.settings.agent_max_model_calls
                if self.agent_runtime
                else self.settings.max_llm_calls
            ),
            tool_limiter=self.tool_limiter,
            started=started,
            on_result=self._on_result,
            runtime_guard=self.runtime_guard,
            prompts=self.prompts,
            prompt_binding=prompt_binding,
            frontend_context=dict(request.context),
        )
        await ctx.events.accepted()
        run = ApplicationRun(record, emitter, context=ctx)
        self.active[record.run_id] = run
        run.task = asyncio.create_task(self._run(ctx), name="metaagent:" + record.run_id)
        return run

    async def execute(self, request: ApplicationRequest) -> ApplicationRun:
        run = await self.start(request)
        if run.task:
            await run.task
        return run

    async def reuse_request(
        self, owner: str, request_id: str, digest: str
    ) -> ApplicationRun | None:
        async with self.locks.hold("request:" + fingerprint([owner, request_id])):
            existing = await self.repository.request_run(owner, request_id)
            if existing is None:
                return None
            if existing.request_hash != digest:
                raise DomainError("idempotency_conflict", "同一请求编号已用于不同内容。")
            run = await self.owned_run(owner, existing.run_id)
            if run is None:
                raise DomainError("run_expired", "运行已过期，请使用新的请求编号。")
            return ApplicationRun(run.record, run.emitter, reused=True)

    async def owned_run(self, owner: str, run_id: str) -> ApplicationRun | None:
        mapping = await self.repository.get("agent_runs", owner, run_id)
        if mapping is None:
            return None
        scope = mapping["scope_key"]
        async with self.locks.hold("recover:" + run_id):
            active = self.active.get(run_id)
            if active:
                if active.record.scope_key != scope:
                    return None
                await active.emitter.snapshot()
                return active
            record = await self.repository.run(scope, run_id)
            if record is None:
                return None
            emitter = EventEmitter(record, self.repository)
            if not any(e.type == "completed" for e in record.events):
                # A restarted process cannot resume side effects. Preserve replayable history.
                record.status = "cancelled"
                record.goal_statuses = {
                    g: "cancelled"
                    for g in (record.plan.goal_ids if record.plan else record.goal_statuses)
                }
                for action in record.action_delivery:
                    record.action_delivery[action] = "delivery_unknown"
                if record.plan:
                    for task in record.plan.tasks:
                        record.results.setdefault(
                            task.task_id,
                            TaskResult(
                                task_id=task.task_id,
                                status="cancelled",
                                code="process_interrupted",
                                message="服务中断，请发起新请求。",
                                attempts=0,
                            ),
                        )
                publisher = RunEventPublisher(emitter)
                if not record.events:
                    await publisher.accepted()
                await publisher.completed(self.completion(record))
            await emitter.finish()
            return ApplicationRun(record, emitter, reused=True)

    async def acknowledge_action(
        self, run: ApplicationRun, action_id: str, status: str, reason: str | None
    ) -> dict[str, Any]:
        async with self.locks.hold(f"ack:{run.record.run_id}:{action_id}"):
            record = await run.emitter.snapshot()
            if not any(
                e.type == "action_ready" and e.payload["action_id"] == action_id
                for e in record.events
            ):
                raise DomainError("action_not_found", "动作不存在或已过期。")
            key = fingerprint([record.run_id, action_id])
            prior = await self.repository.get("action_acks", record.scope_key, key)
            if prior and (prior["status"] != status or prior["reason"] != reason):
                raise DomainError("ack_conflict", "该动作已有不同的执行确认。")
            if prior is None:
                await self.repository.put(
                    "action_acks",
                    record.scope_key,
                    key,
                    {"status": status, "reason": reason, "created_at": utcnow().isoformat()},
                    self.settings.run_ttl_seconds,
                )
            return {"accepted": True, "action_id": action_id, "status": status}

    # 核心代码

    async def _run(self, ctx: RunContext) -> None:
        try:
            await self._execute_run(ctx)
        except EventPersistenceError:
            self._settle(ctx, "failed", "event_persistence_failed", "运行记录暂不可用。")
        finally:
            ctx.completing = True
            try:
                await self._complete(ctx)
            except EventPersistenceError:
                self._settle(ctx, "failed", "event_persistence_failed", "运行记录暂不可用。")
                ctx.record.status = "failed"
            finally:
                await ctx.emitter.finish()
                self._release_run(ctx.record.run_id)

    async def _execute_run(self, ctx: RunContext) -> None:
        # fix：生成一个用于定位业务会话的标识符，多轮会话共享conversation_id，但是有自己的run_id
        # ctx是上下文管理
        thread = ctx.scope.thread_id(ctx.record.conversation_id)
        try:
            # 限制当前请求的剩余执行时间
            async with asyncio.timeout(ctx.remaining):
                # 限制请求并发量，并确保同一业务会话串行处理，锁处理
                async with self.request_limiter, self.locks.hold("conversation:" + thread):
                    # fix:上下文中记忆部分的读取
                    ctx.memory = await self.repository.conversation(thread)
                    await prepare_frontend_context(ctx)
                    # 上下文填入
                    if self.agent_runtime:
                        await self.agent_runtime.execute(ctx)
                    # 不同的执行路径：
                    # self.agent_runtime 使用当前单 Agent Runtime，
                    # 让模型在 LangChain/LangGraph 中循环调用工具
                    # graph：之前的planner
                    elif self.graph:
                        await self._enrich_patient_brief(ctx)
                        await self.graph.ainvoke(
                            {"run_id": ctx.record.run_id, "stage": "accepted"},
                            config={"configurable": {"thread_id": ctx.record.run_id}},
                            context=ctx,
                        )
                    else:
                        await self._enrich_patient_brief(ctx)
                        await self.plan(ctx)
                        await self.execute_plan(ctx)
                    await self._save_memory(ctx, thread)

        # 问题处理类，处理中途取消，超时问题
        except asyncio.CancelledError:
            ctx.interrupted = True
            self._settle(ctx, "cancelled", "request_cancelled", "本次请求已取消。")
        except TimeoutError:
            self._settle(ctx, "failed", "request_timeout", "本次请求已达到执行时限。")
            await ctx.events.failure(
                FailurePayload(
                    code="request_timeout", text="本次请求已达到执行时限。", retryable=False
                )
            )
        # 处理所属错误问题
        except DomainError as exc:
            self._settle(ctx, exc.outcome, exc.code, exc.message)
            if exc.outcome == "clarification":
                await ctx.events.clarification(ClarificationPayload(text=safe_text(exc.message)))
            else:
                await ctx.events.failure(
                    FailurePayload(
                        text=safe_text(exc.message), code=exc.code, retryable=exc.retryable
                    )
                )
        except EventPersistenceError:
            raise
        except Exception as exc:
            logger.error(
                "Application failure run_id=%s error_type=%s", ctx.record.run_id, type(exc).__name__
            )
            self._settle(
                ctx, "failed", "application_internal_error", "当前请求处理失败，请稍后重试。"
            )
            await ctx.events.failure(
                FailurePayload(
                    code="application_internal_error",
                    text="当前请求处理失败，请稍后重试。",
                    retryable=False,
                )
            )

    async def _enrich_patient_brief(self, ctx: RunContext) -> None:
        """链首身份识别：患者场景装填基本档案到上下文，失败仅降级不阻断。

        只有可信注入的 patient_id 存在时才发起；后端接口不可用或预算耗尽
        时保持 patient_brief=None，会话继续走无档案路径。装填响应落证据库，
        供概况类任务以事实投影引用。
        """
        if (
            not ctx.scope.patient_id
            or not ctx.settings.multisource_patient_context_enabled
            or conversational_only(ctx.query)
        ):
            return
        try:
            body = await ctx.call(
                "get_multisource_patient_context",
                {
                    "system_context": {"user": ctx.scope.patient_id},
                    # purpose 标记链首装填调用，与领域工作流同端点调用区分
                    # （计量与测试隔离用）；后端忽略未知请求字段。
                    "purpose": "enrichment",
                    "projection_level": "compact",
                    "force_refresh": False,
                },
            )
        except DomainError as exc:
            logger.warning(
                "patient brief enrichment failed code=%s run_id=%s",
                exc.code,
                ctx.record.run_id,
            )
            return
        brief = brief_from_context_response(
            body, include_name=ctx.settings.patient_brief_include_name
        )
        if brief.is_patient:
            try:
                evidence = await ctx.repository.save_evidence(
                    ctx.scope.scope_hash,
                    "get_multisource_patient_context",
                    ctx.record.request_id,
                    body,
                    "1.6.0",
                )
                brief = replace(
                    brief,
                    evidence_id=evidence.evidence_id,
                    source_version=evidence.source_version,
                )
            except Exception:
                logger.warning(
                    "patient brief evidence save failed run_id=%s",
                    ctx.record.run_id,
                    exc_info=True,
                )
        ctx.patient_brief = brief

    async def plan(self, ctx: RunContext) -> None:
        await ctx.events.progress("planning")
        brief = ctx.patient_brief
        decision = await self.planner.parse(
            ctx.query,
            ctx.memory,
            ctx.llm_budget,
            patient_brief=brief.display_text if brief and brief.is_patient else None,
            prompt_binding=ctx.prompt_binding,
            prompt_usages=ctx.record.metrics.setdefault("prompt_usages", []),
        )
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
                await ctx.events.clarification(
                    ClarificationPayload(
                        text=safe_text(decision.decision_summary or "请进一步明确您的请求。"),
                        missing_slots=list(
                            dict.fromkeys(slot for g in decision.goals for slot in g.missing_slots)
                        ),
                    )
                )
            else:
                # No tool evidence: only bounded conversational/capability language is allowed.
                text = (
                    "你好，我可以协助查询训练、医生和场景信息。"
                    if decision.decision == "respond"
                    else "该领域或操作暂未支持。"
                )
                await ctx.events.answer(AnswerContent(text=text, fact_ids=[], doc_refs=[]))
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
                await ctx.events.clarification(
                    ClarificationPayload(
                        text=safe_text(message), missing_slots=ctx.goals[gid].missing_slots
                    ),
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
                await ctx.events.clarification(
                    ClarificationPayload(text=exc.message, missing_slots=[]), task_id=task.task_id
                )
            else:
                await ctx.events.failure(
                    FailurePayload(text=exc.message, code=exc.code, retryable=False),
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
                and gid in ctx.events.answers
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
                    domain=result.outputs.get("record_domain", "irego"),
                )
            if "history_page" in result.outputs:
                ctx.memory.history_page = result.outputs["history_page"]
                ctx.memory.history_seen = True
                ctx.memory.history_domain = result.outputs.get("record_domain", "irego")
        if anchors:
            ctx.memory.current_record = next(iter(anchors.values())) if len(anchors) == 1 else None
        ctx.memory.pending_goals = [
            g
            for gid, g in ctx.goals.items()
            if ctx.record.goal_statuses.get(gid) == "clarification"
        ]
        answers = "\n".join(e.payload["text"] for e in ctx.events.answers.values())
        if not answers:
            answers = "\n".join(
                e.payload["text"]
                for e in ctx.record.events
                if e.type in {"answer_part", "clarification"}
            )
        ctx.memory.turns.append({"user": ctx.query, "assistant": answers})
        await self.repository.save_conversation(thread, ctx.memory)

    @staticmethod
    def completion(record: RunRecord) -> RunOutcome:
        return RunOutcome(
            outcome=record.status,
            goal_statuses=record.goal_statuses,
            task_statuses={tid: result.status for tid, result in record.results.items()},
        )

    async def _complete(self, ctx: RunContext) -> None:
        if ctx.emitter.persistence_failed:
            self._settle(ctx, "failed", "event_persistence_failed", "运行记录暂不可用。")
            ctx.record.status = "failed"
            return
        if ctx.record.status == "running":
            ctx.record.status = aggregate_status(list(ctx.record.goal_statuses.values()))
        ctx.record.metrics.update(
            elapsed_ms=(time.monotonic() - ctx.started) * 1000,
            tool_calls=ctx.tool_calls,
            llm_calls=ctx.llm_budget.calls,
            provider_usage=ctx.llm_budget.usage,
            token_count_method="estimated_unless_provider_usage_present",
        )
        await ctx.events.completed(self.completion(ctx.record))
        try:
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
                    else [
                        r.outputs["tool_name"]
                        for r in ctx.record.results.values()
                        if "tool_name" in r.outputs
                    ],
                },
                self.settings.audit_ttl_seconds,
            )
        except Exception:
            logger.warning("Unable to save runtime audit run_id=%s", ctx.record.run_id)

    async def cancel(self, scope: str, run_id: str) -> RunRecord | None:
        run = self.active.get(run_id)
        if run is not None and run.record.scope_key != scope:
            return None
        record = run.record if run else await self.repository.run(scope, run_id)
        if record is None:
            return None
        if run and run.task:
            if not run.task.cancelling() and not (run.context and run.context.completing):
                run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
            if run.record.status == "running" and run.context:
                # Cancellation may occur before the coroutine enters its try/finally.
                self._settle(run.context, "cancelled", "request_cancelled", "本次请求已取消。")
                run.context.completing = True
                try:
                    await self._complete(run.context)
                finally:
                    await run.emitter.finish()
                    self._release_run(run_id)
            return run.record
        return record

    def _release_run(self, run_id: str) -> None:
        run = self.active.pop(run_id, None)
        if run and run.admitted:
            self.admission_limiter.release()

    async def close(self) -> None:
        runs = list(self.active.values())
        await asyncio.gather(
            *(self.cancel(run.record.scope_key, run.record.run_id) for run in runs),
            return_exceptions=True,
        )
