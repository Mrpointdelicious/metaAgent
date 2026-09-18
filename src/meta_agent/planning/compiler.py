"""
创建日期：2026-09-08
文件功能：将用户目标编译成最少依赖任务，验证能力、身份、条件与预算。
"""

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from meta_agent.config import Settings
from meta_agent.contracts import (
    Binding,
    ConversationState,
    DomainError,
    Goal,
    Guard,
    IntentDecision,
    IReGoRequest,
    OutcomeStatus,
    TaskSpec,
    ValidatedPlan,
)
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.planning.capabilities import capability_specs
from meta_agent.planning.parser import CONDITIONAL, NEGATIVE, REPORT_NEGATION


@dataclass
class Compilation:
    plan: ValidatedPlan
    dispositions: dict[str, tuple[OutcomeStatus, str]] = field(default_factory=dict)


class PlanCompiler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.specs = capability_specs(settings)

    def compile(
        self,
        decision: IntentDecision,
        query: str,
        scope: TrustedScope,
        memory: ConversationState,
        request_id: str,
        *,
        anchor_fresh: bool = False,
    ) -> Compilation:
        plan = ValidatedPlan(
            request_id=request_id,
            goal_ids=[g.goal_id for g in decision.goals],
            deadline_ms=int(self.settings.request_timeout_seconds * 1000),
            outcome_hint=decision.decision,
        )
        result = Compilation(plan)
        ids = plan.goal_ids
        if len(ids) != len(set(ids)) or len(ids) > self.settings.max_goals:
            raise DomainError(
                "invalid_goals", "目标编号重复或超过本轮预算。", outcome="clarification"
            )
        goals = {g.goal_id: g for g in decision.goals}
        if decision.decision == "execute" and not goals:
            raise DomainError("empty_goals", "请明确本轮需要执行的目标。", outcome="clarification")
        if decision.decision in {"respond", "clarify", "unsupported"}:
            outcome = {
                "respond": "succeeded",
                "clarify": "clarification",
                "unsupported": "unsupported",
            }
            result.dispositions = {
                g.goal_id: (outcome[decision.decision], decision.decision_summary)
                for g in decision.goals
            }
            return result
        by_goal: dict[str, list[str]] = {gid: [] for gid in ids}
        resolves: dict[str, TaskSpec] = {}

        def add(
            gids: list[str],
            capability: str,
            arguments: dict[str, Any] | None = None,
            bindings: list[Binding] | None = None,
            deps: list[str] | None = None,
        ) -> TaskSpec:
            spec = self.specs[capability]
            task = TaskSpec(
                task_id=f"t{len(plan.tasks) + 1}",
                goal_ids=gids,
                capability=capability,
                arguments=arguments or {},
                bindings=bindings or [],
                depends_on=deps or [],
                effect=spec.effect,
                priority_class=(
                    "action"
                    if capability.startswith("scene.")
                    else "artifact"
                    if spec.effect == "prepare_artifact"
                    else "interactive"
                ),
                timeout_ms=int(
                    1000
                    * (
                        self.settings.report_timeout_seconds
                        if spec.effect == "prepare_artifact"
                        else self.settings.tool_timeout_seconds
                    )
                ),
                retry_limit=1 if spec.effect == "read" else 0,
                idempotency_key=f"{request_id}:{'-'.join(gids)}:{capability}:{len(plan.tasks)}",
            )
            plan.tasks.append(task)
            for gid in gids:
                by_goal[gid].append(task.task_id)
            return task

        def bind(task: TaskSpec, name: str = "session_ref") -> Binding:
            return Binding(
                argument=name, source_kind="task_output", source_id=task.task_id, field=name
            )

        def record_binding(g: Goal) -> tuple[Binding, list[str]]:
            selector = g.selector.model_copy(deep=True)
            if selector.mode == "none":
                selector.mode = "latest_record"
            if selector.mode == "current_ref" and memory.current_record and anchor_fresh:
                return Binding(
                    argument="session_ref",
                    source_kind="evidence_index",
                    source_id="current",
                    field="session_ref",
                ), []
            key = selector.model_dump_json()
            if key not in resolves:
                resolves[key] = add(
                    [g.goal_id], "rehab.resolve_session", {"selector": selector.model_dump()}
                )
            else:
                task = resolves[key]
                if g.goal_id not in task.goal_ids:
                    task.goal_ids.append(g.goal_id)
                    by_goal[g.goal_id].append(task.task_id)
            return bind(resolves[key]), [resolves[key].task_id]

        scene_goals = []
        scene_cursor = 0
        for g in decision.goals:
            gid = g.goal_id
            # Original spans are an authorization check, not merely a label generated by a model.
            if not g.query_span or g.query_span not in query:
                result.dispositions[gid] = (
                    "clarification",
                    "无法把目标对应到您的原话，请重新明确该目标。",
                )
                continue
            if g.selector.candidate_ref not in (None, "current"):
                result.dispositions[gid] = ("clarification", "记录引用必须来自当前授权会话。")
                continue
            # after_goal_ids / condition 不再参与执行依赖：LLM 无权生成依赖边。
            if g.missing_slots or g.clarification:
                result.dispositions[gid] = (
                    "clarification",
                    g.clarification or "请补充该目标所需的信息。",
                )
                continue
            if g.domain in {"iremo", "iretour", "hospital"} or g.kind == "unsupported":
                result.dispositions[gid] = ("unsupported", "该领域接口暂未接入。")
                continue
            is_rehab = g.kind.startswith("rehab_") or g.kind in {"report", "irego"}
            if is_rehab and (g.domain != "irego" or not scope.patient_id):
                result.dispositions[gid] = ("clarification", "请先通过患者端绑定有效患者身份。")
                continue
            if g.kind in {"scene_action", "doctor_query"} and not scope.space_id:
                result.dispositions[gid] = ("clarification", "请先提供当前空间上下文。")
                continue
            if g.kind == "scene_action" and g.domain != "scene":
                result.dispositions[gid] = ("clarification", "场景动作的领域不一致。")
                continue
            if g.kind == "scene_action":
                start = query.find(g.query_span, scene_cursor)
                if start < 0:
                    result.dispositions[gid] = (
                        "clarification",
                        "动作未按原文出现顺序对应，请明确各个动作。",
                    )
                    continue
                scene_cursor = start + len(g.query_span)
                prefix = query[max(0, start - 6) : start]
                quoted = any(
                    match.start() <= start < match.end()
                    for match in re.finditer(r'“[^”]*”|"[^"]*"|「[^」]*」', query)
                )
                if quoted:
                    result.dispositions[gid] = (
                        "clarification",
                        "引述中的动作未视为执行授权，请明确要执行的内容。",
                    )
                    continue
                if (
                    "action" in g.excluded_outputs
                    or NEGATIVE.search(g.query_span)
                    or NEGATIVE.search(prefix)
                ):
                    result.dispositions[gid] = ("skipped_condition", "已按要求不执行该动作。")
                    continue
                if CONDITIONAL.search(query) and not g.condition:
                    result.dispositions[gid] = ("clarification", "动作条件尚未明确，暂不发送。")
                    continue
                if not self.settings.scene_enabled or not self.settings.scene_actions_enabled:
                    result.dispositions[gid] = ("unsupported", "场景动作输出尚未启用。")
                    continue
                if re.search(r"那个|那里|那边|这个", g.query_span):
                    result.dispositions[gid] = ("clarification", "请明确场景目标名称。")
                    continue
                scene_goals.append(g)
                continue
            if g.kind == "chat":
                result.dispositions[gid] = ("succeeded", "我可以协助查询训练、医生和场景信息。")
            elif g.kind == "doctor_query":
                if not self.settings.doctors_enabled:
                    result.dispositions[gid] = ("unsupported", "医生查询尚未启用。")
                else:
                    add([gid], "doctors.search")
            elif g.kind == "knowledge_query":
                if (
                    g.domain not in {"health", "product", "help"}
                    or not self.settings.knowledge_corpus_path
                ):
                    result.dispositions[gid] = ("unavailable", "尚无已批准的对应知识语料。")
                else:
                    add([gid], "knowledge.search", {"query": g.query_span, "domain": g.domain})
            elif g.kind in {
                "irego",
                "rehab_overview",
                "rehab_history",
                "rehab_session",
                "rehab_trend",
                "report",
            }:
                request = self._irego_request(g, query)
                if request is None:
                    result.dispositions[gid] = ("clarification", "缺少 IREGO 业务操作请求。")
                    continue
                if (
                    request.operation == "session"
                    and request.selector.mode in {"current_ref", "previous_record"}
                    and not memory.current_record
                ):
                    result.dispositions[gid] = (
                        "clarification",
                        "还没有明确的当前训练记录，请先说明是哪次训练。",
                    )
                    continue
                task = add([gid], "irego.execute", request.model_dump(exclude_none=True))
                # 固定 Workflow 自带步骤级重试与报表时限；调度器不再重试整个宏任务。
                task.retry_limit = 0
                if request.need_artifact:
                    task.timeout_ms = int(1000 * self.settings.report_timeout_seconds)
                    task.priority_class = "artifact"
            else:
                result.dispositions[gid] = ("unsupported", "该目标尚无可执行能力。")

        # Matching can be batched; delivery remains independently guarded per original occurrence.
        batches = [[g] for g in scene_goals if g.condition]
        unconditional = [g for g in scene_goals if not g.condition]
        if unconditional:
            batches.insert(0, unconditional)
        for batch in batches:
            target = "，".join(g.query_span for g in batch)
            resolve = add([g.goal_id for g in batch], "scene.resolve", {"target": target})
            for g in batch:
                deps = [resolve.task_id]
                task = add(
                    [g.goal_id],
                    "scene.dispatch",
                    bindings=[
                        Binding(
                            argument="action_ref",
                            source_kind="task_output",
                            source_id=resolve.task_id,
                            field="action_ref",
                            item_key=g.goal_id,
                        )
                    ],
                    deps=deps,
                )

        # 只有场景动作保留代码级条件接线（Guard 机制）；iReGo 等域不再读取 LLM 依赖声明。
        for g in decision.goals:
            own = by_goal[g.goal_id]
            if not own or not g.condition:
                continue
            if g.kind != "scene_action":
                result.dispositions[g.goal_id] = (
                    "clarification",
                    "该条件无法可靠判断，请明确条件。",
                )
                continue
            predicate = self._predicate(g.condition.text)
            source_ids = g.condition.source_goal_ids
            if (
                predicate is None
                or not source_ids
                or set(source_ids) - set(ids)
                or g.goal_id in source_ids
            ):
                result.dispositions[g.goal_id] = (
                    "clarification",
                    "该条件无法可靠判断，请明确条件。",
                )
                continue
            providers = {
                "has_results": {"doctors.search", "rehab.history"},
                "no_results": {"doctors.search"},
                "record_completed": {"rehab.session", "rehab.resolve_session"},
                "report_available": {"rehab.session"},
                "scene_context_confirmed": {"scene.resolve"},
            }
            sources = []
            for before in source_ids:
                matches = [
                    tid
                    for tid in by_goal[before]
                    if tid not in own
                    and next(t for t in plan.tasks if t.task_id == tid).capability
                    in providers[predicate]
                ]
                if matches:
                    sources.append(matches[-1])
            if len(sources) != len(source_ids):
                sources = []
            if not sources:
                result.dispositions[g.goal_id] = ("clarification", "缺少判断条件的前置查询。")
                continue
            for tid in own:
                task = next(t for t in plan.tasks if t.task_id == tid)
                task.depends_on = list(dict.fromkeys(task.depends_on + sources))
                task.guards += [
                    Guard(source_task_id=source, predicate=predicate) for source in sources
                ]
        # Propagate removed scene-condition sources before pruning shared tasks.
        changed = True
        while changed:
            changed = False
            for g in decision.goals:
                dependencies = g.condition.source_goal_ids if g.condition else []
                if g.goal_id not in result.dispositions and any(
                    dep in result.dispositions and result.dispositions[dep][0] != "succeeded"
                    for dep in dependencies
                ):
                    result.dispositions[g.goal_id] = (
                        "blocked_dependency",
                        "前置目标未完成，该目标暂不执行。",
                    )
                    changed = True
        blocked = set(result.dispositions)
        plan.tasks = [t for t in plan.tasks if not set(t.goal_ids) <= blocked]
        # Remove blocked members from shared read goals; never execute their dispatch.
        for t in plan.tasks:
            t.goal_ids = [gid for gid in t.goal_ids if gid not in blocked]
            if t.capability == "scene.resolve":
                t.arguments["target"] = "，".join(goals[gid].query_span for gid in t.goal_ids)
        if len(plan.tasks) > self.settings.max_tasks:
            plan.tasks = []
            plan.outcome_hint = "clarify"
            result.dispositions = {
                gid: ("clarification", "本轮任务超出执行预算，请分批处理。") for gid in ids
            }
        self.validate(plan, scope, memory)
        return result

    @staticmethod
    def _irego_request(g: Goal, query: str) -> "IReGoRequest | None":
        """把 Planner 目标收敛成唯一高层业务请求；旧 Goal 形态做兼容推导。"""
        if g.irego is not None:
            request = g.irego.model_copy(deep=True)
        else:
            operation = {
                "rehab_overview": "overview",
                "rehab_history": "history",
                "rehab_session": "session",
                "rehab_trend": "trend",
                "report": "session",
            }.get(g.kind)
            if operation is None:
                return None
            request = IReGoRequest(
                operation=operation,
                selector=g.selector.model_copy(deep=True),
                topics=list(g.topics),
                need_artifact=g.output in {"artifact", "answer_and_artifact"},
            )
        request.force_refresh = bool(re.search(r"刷新|重新", g.query_span))
        # 用户明确不生成报告时强制降级；报告只由 need_artifact 控制。
        if request.need_artifact and (
            "artifact" in g.excluded_outputs or REPORT_NEGATION.search(query)
        ):
            request.need_artifact = False
        return request

    @staticmethod
    def _predicate(text: str) -> str | None:
        if re.search(r"没有.*医生|无.*医生", text):
            return "no_results"
        if re.search(r"有.*医生", text):
            return "has_results"
        if re.search(r"未完成|没.*完成|不.*完成", text):
            return None
        if "完成" in text:
            return "record_completed"
        if re.search(r"报告.*(?:可用|存在)|有.*报告", text):
            return "report_available"
        return None

    def validate(self, plan: ValidatedPlan, scope: TrustedScope, memory: ConversationState) -> None:
        tasks = {task.task_id: task for task in plan.tasks}
        if len(tasks) != len(plan.tasks):
            raise DomainError("duplicate_task", "任务编号重复。", outcome="clarification")

        def ancestors(tid: str, path: set[str]) -> set[str]:
            if tid in path or tid not in tasks:
                raise DomainError(
                    "invalid_dependency", "任务依赖存在循环或缺失。", outcome="clarification"
                )
            deps = set(tasks[tid].depends_on)
            for dep in tasks[tid].depends_on:
                deps |= ancestors(dep, path | {tid})
            return deps

        for task in plan.tasks:
            spec = self.specs.get(task.capability)
            if spec is None or not spec.enabled or task.effect != spec.effect:
                raise DomainError(
                    "capability_disabled", "能力未启用或执行类型不匹配。", outcome="unsupported"
                )
            if (
                spec.requires_patient
                and not scope.patient_id
                or spec.requires_space
                and not scope.space_id
            ):
                raise DomainError(
                    "scope_required", "缺少该能力需要的可信身份。", outcome="clarification"
                )
            deps = ancestors(task.task_id, set())
            args = dict(task.arguments)
            for binding in task.bindings:
                if binding.source_kind == "evidence_index":
                    if (
                        binding.source_id != "current"
                        or binding.field != "session_ref"
                        or not memory.current_record
                    ):
                        raise DomainError(
                            "invalid_binding", "记录绑定无效。", outcome="clarification"
                        )
                elif (
                    binding.source_id not in deps
                    or binding.field not in self.specs[tasks[binding.source_id].capability].provides
                ):
                    raise DomainError(
                        "invalid_binding", "任务输入引用无效。", outcome="clarification"
                    )
                if binding.argument in args:
                    raise DomainError(
                        "duplicate_argument", "参数存在冲突来源。", outcome="clarification"
                    )
                args[binding.argument] = "__bound__"
            if any(guard.source_task_id not in deps for guard in task.guards):
                raise DomainError(
                    "invalid_guard", "条件来源必须是前置任务。", outcome="clarification"
                )
            try:
                spec.arguments.model_validate(args)
            except ValidationError as exc:
                raise DomainError(
                    "invalid_arguments", "工具参数不符合能力契约。", outcome="clarification"
                ) from exc
