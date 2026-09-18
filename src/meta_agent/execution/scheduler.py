"""
创建日期：2026-09-11
文件功能：
执行 PlanCompiler 生成的 ValidatedPlan 任务 DAG。

职责：
1. 解析任务间 Binding。
2. 校验 Guard。
3. 根据 depends_on 动态调度任务。
4. 支持有限并发。
5. 支持任务级 timeout / retry。
6. 隔离单任务失败，返回 TaskResult。
7. 阻止依赖失败后的下游任务继续执行。
8. 保存 TaskResult 到 RunRecord。

本模块不负责：
- 理解用户意图。
- 生成任务计划。
- 修改任务依赖。
- 选择具体工具。
- 生成最终回答。
- 向前端发送事件。
"""

from __future__ import annotations

import asyncio
import logging
import time

from meta_agent.application.context import RunContext
from meta_agent.contracts import (
    DomainError,
    OutcomeStatus,
    TaskResult,
    TaskSpec,
)
from meta_agent.execution.dispatcher import DomainDispatcher

logger = logging.getLogger(__name__)


# 只有下面两个状态允许依赖任务继续执行。
#
# partial：
# 上游可能只完成了一部分目标，但仍可能提供了当前任务需要的输出。
#
# 例如：
# scene.resolve 同时解析两个动作：
# - 一个 matched
# - 一个 ambiguous
#
# resolve TaskResult 可以是 partial，
# 但 matched 动作对应的 scene.dispatch 仍然应该继续。
CONTINUABLE_DEPENDENCY_STATUSES: frozenset[OutcomeStatus] = frozenset(
    {
        "succeeded",
        "partial",
    }
)


# ready task 的调度优先级。
#
# 只在“已经满足全部依赖”的任务之间排序，
# 不改变任何 DAG 依赖关系。
#
# 这样可以尽量做到：
# action > interactive > artifact
#
# 例如：
# 打开面板 + 查询训练 + 生成图片
#
# 场景动作不需要等待慢报表任务。
PRIORITY_ORDER = {
    "action": 0,
    "interactive": 1,
    "artifact": 2,
}


def resolve_arguments(
    task: TaskSpec,
    ctx: RunContext,
) -> dict:
    """
    将 TaskSpec.arguments 与 bindings 合并成领域 Adapter 的最终参数。

    这里只消费 PlanCompiler 已经产生并验证过的 Binding。
    Scheduler 不允许自行推断新的参数或依赖。
    """

    arguments = dict(task.arguments)

    for binding in task.bindings:
        # ------------------------------------------------------------
        # 1. 当前会话证据索引
        # ------------------------------------------------------------
        if binding.source_kind == "evidence_index":
            if binding.source_id != "current":
                raise DomainError(
                    "invalid_evidence_binding",
                    "当前执行器只允许读取已授权的 current 会话引用。",
                    outcome="clarification",
                )

            anchor = ctx.memory.current_record

            if anchor is None:
                raise DomainError(
                    "anchor_missing",
                    "当前会话没有可复用的训练记录引用。",
                    outcome="clarification",
                )

            if binding.field == "session_ref":
                value = anchor.session_ref

            elif binding.field == "evidence_id":
                value = anchor.evidence_id

            else:
                raise DomainError(
                    "unsupported_evidence_binding",
                    f"当前记录锚点不提供字段：{binding.field}",
                    outcome="clarification",
                )

        # ------------------------------------------------------------
        # 2. 前置任务输出
        # ------------------------------------------------------------
        elif binding.source_kind == "task_output":
            parent = ctx.record.results.get(binding.source_id)

            if parent is None:
                raise DomainError(
                    "binding_parent_missing",
                    f"前置任务结果不存在：{binding.source_id}",
                )

            if binding.field not in parent.outputs:
                raise DomainError(
                    "binding_field_missing",
                    (f"前置任务 {binding.source_id} 没有提供字段：{binding.field}"),
                )

            value = parent.outputs[binding.field]

            # scene.resolve 等任务可能返回：
            #
            # action_ref = {
            #     "g1": "...",
            #     "g2": "..."
            # }
            #
            # item_key 用于取当前 goal 对应的值。
            if binding.item_key is not None:
                if not isinstance(value, dict):
                    raise DomainError(
                        "binding_item_invalid",
                        (f"任务 {binding.source_id} 输出 {binding.field} 不是可索引对象。"),
                    )

                if binding.item_key not in value:
                    raise DomainError(
                        "binding_item_missing",
                        (f"任务 {binding.source_id} 输出中不存在目标：{binding.item_key}"),
                    )

                value = value[binding.item_key]

        else:
            raise DomainError(
                "invalid_binding_source",
                f"未知 Binding 来源：{binding.source_kind}",
            )

        # PlanCompiler 已经禁止 arguments 和 binding 同名冲突。
        # Scheduler 再保留一道 fail-safe。
        if binding.argument in arguments:
            raise DomainError(
                "duplicate_argument",
                f"任务参数重复绑定：{binding.argument}",
            )

        arguments[binding.argument] = value

    return arguments


def evaluate_guards(
    task: TaskSpec,
    ctx: RunContext,
) -> tuple[bool, str | None]:
    """
    校验任务的运行条件。

    返回：
        (True, None)
            条件全部满足，可以运行。

        (False, "skip")
            条件不满足，任务应 skipped_condition。

        (False, "clarify")
            条件无法满足，需要 clarification。
    """

    for guard in task.guards:
        source = ctx.record.results.get(guard.source_task_id)

        if source is None:
            raise DomainError(
                "guard_source_missing",
                f"Guard 前置任务不存在：{guard.source_task_id}",
            )

        # Guard predicate 与领域 Adapter 的 outputs key 对齐：
        #
        # doctors.search:
        #   has_results
        #   no_results
        #
        # rehab.session:
        #   record_completed
        #   report_available
        #
        # scene.resolve:
        #   scene_context_confirmed
        #
        raw_value = source.outputs.get(guard.predicate)

        if type(raw_value) is not bool:
            return False, "clarify"
        passed = raw_value is True

        if not passed:
            return False, guard.on_false

    return True, None


def dependency_failure(
    task: TaskSpec,
    ctx: RunContext,
) -> list[str]:
    """
    返回已经确定失败、因此阻止当前任务执行的依赖。

    未完成的依赖不会出现在这里。
    """

    blocked_by: list[str] = []

    for dependency_id in task.depends_on:
        result = ctx.record.results.get(dependency_id)

        if result is None:
            continue

        if result.status not in CONTINUABLE_DEPENDENCY_STATUSES:
            blocked_by.append(dependency_id)

    return blocked_by


def dependencies_completed(
    task: TaskSpec,
    ctx: RunContext,
) -> bool:
    """判断任务的所有依赖是否已经产生 TaskResult。"""

    return all(dependency_id in ctx.record.results for dependency_id in task.depends_on)


class TaskScheduler:
    """
    执行 ValidatedPlan 中的动态任务 DAG。

    Scheduler 只处理执行语义：

        dependency
        binding
        guard
        timeout
        retry
        concurrency
        TaskResult

    它不参与 Planner 和领域业务判断。
    """

    def __init__(
        self,
        dispatcher: DomainDispatcher,
    ) -> None:
        self.dispatcher = dispatcher

    async def _execute_task(
        self,
        task: TaskSpec,
        ctx: RunContext,
    ) -> TaskResult:
        """
        执行单个 TaskSpec。

        DomainError 会转换为 TaskResult，
        不允许一个普通领域任务失败直接导致整个 Run 崩溃。
        """

        started = time.monotonic()
        attempts = 0

        # ------------------------------------------------------------
        # Guard
        # ------------------------------------------------------------
        try:
            guard_ok, guard_action = evaluate_guards(
                task,
                ctx,
            )

        except DomainError as exc:
            return TaskResult(
                task_id=task.task_id,
                status=exc.outcome,
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                attempts=0,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        if not guard_ok:
            if guard_action == "clarify":
                return TaskResult(
                    task_id=task.task_id,
                    status="clarification",
                    code="guard_not_satisfied",
                    message="执行条件尚未满足，需要进一步确认。",
                    attempts=0,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )

            return TaskResult(
                task_id=task.task_id,
                status="skipped_condition",
                code="guard_not_satisfied",
                message="执行条件未满足，本任务未执行。",
                attempts=0,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        # ------------------------------------------------------------
        # Binding
        # ------------------------------------------------------------
        try:
            arguments = resolve_arguments(
                task,
                ctx,
            )

        except DomainError as exc:
            return TaskResult(
                task_id=task.task_id,
                status=exc.outcome,
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                attempts=0,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        # ------------------------------------------------------------
        # Domain execution + retry
        # ------------------------------------------------------------
        while True:
            attempts += 1

            try:
                remaining = ctx.remaining

                timeout_seconds = min(
                    task.timeout_ms / 1000.0,
                    remaining,
                )

                if timeout_seconds <= 0:
                    raise TimeoutError

                async with asyncio.timeout(timeout_seconds):
                    result = await self.dispatcher.execute(
                        task,
                        arguments,
                        ctx,
                    )

                result.attempts = attempts
                result.elapsed_ms = (time.monotonic() - started) * 1000

                return result

            except asyncio.CancelledError:
                # Scheduler / Request 被取消时必须真正传播取消。
                raise

            except DomainError as exc:
                # retry_limit 表示允许的额外重试次数。
                #
                # retry_limit = 1：
                # 第一次失败后允许再执行一次。
                if (
                    exc.retryable
                    and task.effect == "read"
                    and attempts <= task.retry_limit
                    and ctx.remaining > 0
                ):
                    await asyncio.sleep(
                        min(
                            0.2 * attempts,
                            0.5,
                        )
                    )
                    continue

                return TaskResult(
                    task_id=task.task_id,
                    status=exc.outcome,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                    attempts=attempts,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )

            except TimeoutError:
                return TaskResult(
                    task_id=task.task_id,
                    status="failed",
                    code="task_timeout",
                    message="任务执行超时。",
                    retryable=False,
                    attempts=attempts,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )

            except Exception:
                # 非预期异常不能把具体内部异常文本暴露给患者。
                # 日志中保留 traceback，TaskResult 使用稳定错误分类。
                logger.exception(
                    "Unhandled task execution error",
                    extra={
                        "run_id": ctx.record.run_id,
                        "task_id": task.task_id,
                        "capability": task.capability,
                    },
                )

                return TaskResult(
                    task_id=task.task_id,
                    status="failed",
                    code="task_internal_error",
                    message="任务执行发生内部错误。",
                    retryable=False,
                    attempts=attempts,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )

    async def _persist_result(
        self,
        task: TaskSpec,
        result: TaskResult,
        ctx: RunContext,
    ) -> None:
        """统一写入 RunRecord 并持久化。"""

        ctx.record.results[task.task_id] = result

        await ctx.repository.save_run(ctx.record)

        if ctx.on_result is not None:
            await ctx.on_result(task, result, ctx)

        logger.info(
            (
                "Task finished: "
                "run_id=%s task_id=%s capability=%s "
                "status=%s attempts=%s elapsed_ms=%.2f"
            ),
            ctx.record.run_id,
            task.task_id,
            task.capability,
            result.status,
            result.attempts,
            result.elapsed_ms,
        )

    async def _cancel_running(
        self,
        running: dict[str, asyncio.Task[TaskResult]],
    ) -> None:
        """取消并回收所有正在运行的 asyncio Task。"""

        if not running:
            return

        for future in running.values():
            future.cancel()

        await asyncio.gather(
            *running.values(),
            return_exceptions=True,
        )

        running.clear()

    async def execute(
        self,
        tasks: list[TaskSpec] | tuple[TaskSpec, ...],
        ctx: RunContext,
    ) -> dict[str, TaskResult]:
        """
        动态执行任务 DAG。

        与简单 wave/gather 不同：

        t1 ──► t3
        t2 ───────── long running

        当 t1 完成后，如果仍有并发槽，
        t3 可以立即开始，不需要等待 t2 完成。
        """

        # ------------------------------------------------------------
        # 基本完整性检查
        # ------------------------------------------------------------
        task_map = {task.task_id: task for task in tasks}

        if len(task_map) != len(tasks):
            raise DomainError(
                "duplicate_task",
                "执行计划包含重复任务编号。",
                outcome="clarification",
            )

        known_ids = set(task_map)

        for task in tasks:
            missing_dependencies = set(task.depends_on) - known_ids

            if missing_dependencies:
                raise DomainError(
                    "invalid_dependency",
                    (f"任务 {task.task_id} 包含不存在的依赖。"),
                    outcome="clarification",
                )

        # 支持恢复：
        # 已经存在 TaskResult 的任务无需重复执行。
        pending: dict[str, TaskSpec] = {
            task.task_id: task for task in tasks if task.task_id not in ctx.record.results
        }

        running: dict[
            str,
            asyncio.Task[TaskResult],
        ] = {}

        original_order = {task.task_id: index for index, task in enumerate(tasks)}

        try:
            while pending or running:
                # ----------------------------------------------------
                # Request 被上层主动中断
                # ----------------------------------------------------
                if ctx.interrupted:
                    for task_id in running:
                        pending[task_id] = task_map[task_id]

                    await self._cancel_running(running)

                    for task in list(pending.values()):
                        result = TaskResult(
                            task_id=task.task_id,
                            status="cancelled",
                            code="request_interrupted",
                            message="本次请求已取消。",
                            attempts=0,
                        )

                        await self._persist_result(
                            task,
                            result,
                            ctx,
                        )

                        pending.pop(
                            task.task_id,
                            None,
                        )

                    break

                # ----------------------------------------------------
                # 全局 request deadline
                # ----------------------------------------------------
                if ctx.remaining <= 0:
                    running_tasks = {task_id: task_map[task_id] for task_id in running}

                    await self._cancel_running(running)

                    unfinished = {
                        **running_tasks,
                        **pending,
                    }

                    for task in unfinished.values():
                        if task.task_id in ctx.record.results:
                            continue

                        result = TaskResult(
                            task_id=task.task_id,
                            status="failed",
                            code="request_timeout",
                            message="本次请求已达到执行时限。",
                            retryable=False,
                            attempts=0,
                        )

                        await self._persist_result(
                            task,
                            result,
                            ctx,
                        )

                    pending.clear()
                    break

                # ----------------------------------------------------
                # 1. 先处理已经确定被依赖阻断的任务
                # ----------------------------------------------------
                blocked_ids: list[str] = []

                for (
                    task_id,
                    task,
                ) in pending.items():
                    failed_dependencies = dependency_failure(
                        task,
                        ctx,
                    )

                    if not failed_dependencies:
                        continue

                    skipped = all(
                        ctx.record.results[dep].status == "skipped_condition"
                        for dep in failed_dependencies
                    )
                    result = TaskResult(
                        task_id=task.task_id,
                        status="skipped_condition" if skipped else "blocked_dependency",
                        code="guard_not_satisfied" if skipped else "dependency_failed",
                        message=("前置任务未完成，当前任务不再执行。"),
                        outputs={"blocked_by": failed_dependencies},
                        attempts=0,
                    )

                    await self._persist_result(
                        task,
                        result,
                        ctx,
                    )

                    blocked_ids.append(task_id)

                for task_id in blocked_ids:
                    pending.pop(
                        task_id,
                        None,
                    )

                # ----------------------------------------------------
                # 2. 找到所有已经满足依赖的任务
                # ----------------------------------------------------
                ready = [
                    task
                    for task in pending.values()
                    if dependencies_completed(
                        task,
                        ctx,
                    )
                ]

                # 已经满足依赖的任务：
                #
                # action
                # ↓
                # interactive
                # ↓
                # artifact
                #
                # 同 priority 保留 PlanCompiler 原始顺序。
                ready.sort(
                    key=lambda task: (
                        PRIORITY_ORDER.get(
                            task.priority_class,
                            99,
                        ),
                        original_order[task.task_id],
                    )
                )

                # ----------------------------------------------------
                # 3. 填满并发槽
                # ----------------------------------------------------
                available_slots = max(
                    0,
                    (ctx.settings.max_parallel_tasks - len(running)),
                )

                scene_order = [
                    t.task_id
                    for t in sorted(
                        (t for t in tasks if t.capability == "scene.dispatch"),
                        key=lambda t: min(
                            (list(ctx.goals).index(g) for g in t.goal_ids if g in ctx.goals),
                            default=original_order[t.task_id],
                        ),
                    )
                ]
                next_scene = next(
                    (tid for tid in scene_order if tid not in ctx.record.results), None
                )
                for task in ready:
                    if available_slots <= 0:
                        break
                    if task.capability == "scene.dispatch" and task.task_id != next_scene:
                        continue

                    future = asyncio.create_task(
                        self._execute_task(
                            task,
                            ctx,
                        ),
                        name=(f"metaagent:{ctx.record.run_id}:{task.task_id}"),
                    )

                    running[task.task_id] = future

                    pending.pop(
                        task.task_id,
                        None,
                    )
                    available_slots -= 1

                    logger.info(
                        ("Task started: run_id=%s task_id=%s capability=%s"),
                        ctx.record.run_id,
                        task.task_id,
                        task.capability,
                    )

                # ----------------------------------------------------
                # 4. 如果没有 running，却仍有 pending，
                #    表明 DAG 无法推进。
                #
                #    PlanCompiler 理论上已经阻止循环；
                #    这里是执行层 fail-safe。
                # ----------------------------------------------------
                if not running:
                    if pending:
                        for task in list(pending.values()):
                            result = TaskResult(
                                task_id=task.task_id,
                                status="blocked_dependency",
                                code="dependency_deadlock",
                                message=("任务依赖无法继续执行。"),
                                attempts=0,
                            )

                            await self._persist_result(
                                task,
                                result,
                                ctx,
                            )

                        pending.clear()

                    break

                # ----------------------------------------------------
                # 5. 等待任意一个运行任务完成
                #
                # 不等待整批任务，
                # 这是支持 DAG 及时释放下游任务的关键。
                # ----------------------------------------------------
                done, _ = await asyncio.wait(
                    set(running.values()),
                    timeout=ctx.remaining,
                    return_when=(asyncio.FIRST_COMPLETED),
                )

                # wait timeout
                if not done:
                    continue

                completed_ids: list[str] = []

                for (
                    task_id,
                    future,
                ) in running.items():
                    if future not in done:
                        continue

                    task = task_map[task_id]

                    try:
                        result = future.result()

                    except asyncio.CancelledError:
                        result = TaskResult(
                            task_id=task_id,
                            status="cancelled",
                            code="task_cancelled",
                            message="任务已取消。",
                            attempts=0,
                        )

                    # 理论上 _execute_task 已经处理普通异常。
                    # 这里继续保留 executor-level fail-safe。
                    except Exception:
                        logger.exception(
                            ("Unhandled scheduler future error"),
                            extra={
                                "run_id": ctx.record.run_id,
                                "task_id": task_id,
                            },
                        )

                        result = TaskResult(
                            task_id=task_id,
                            status="failed",
                            code=("scheduler_internal_error"),
                            message=("任务调度发生内部错误。"),
                            attempts=0,
                        )

                    await self._persist_result(
                        task,
                        result,
                        ctx,
                    )

                    completed_ids.append(task_id)

                for task_id in completed_ids:
                    running.pop(
                        task_id,
                        None,
                    )

            # --------------------------------------------------------
            # 调度完成
            # --------------------------------------------------------
            ctx.record.metrics["scheduler_task_count"] = len(tasks)

            ctx.record.metrics["scheduler_result_count"] = len(ctx.record.results)

            ctx.record.metrics["tool_calls"] = ctx.tool_calls

            await ctx.repository.save_run(ctx.record)

            return ctx.record.results

        finally:
            await self._cancel_running(running)
