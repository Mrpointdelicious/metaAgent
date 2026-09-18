"""
创建日期：2026-09-11
文件功能：根据已验证 capability 将任务分发到对应领域适配器。
"""

from pydantic import ValidationError

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec
from meta_agent.domains.doctors import DoctorAdapter
from meta_agent.domains.knowledge import KnowledgeAdapter
from meta_agent.domains.rehab import RehabAdapter
from meta_agent.domains.scene import SceneAdapter
from meta_agent.planning.capabilities import capability_specs


class DomainDispatcher:
    """只执行已通过 PlanCompiler 校验的领域任务。"""

    def __init__(self) -> None:
        self._rehab = RehabAdapter()
        self._scene = SceneAdapter()
        self._doctors = DoctorAdapter()
        self._knowledge = KnowledgeAdapter()

    async def execute(
        self,
        task: TaskSpec,
        arguments: dict,
        ctx: RunContext,
    ) -> TaskResult:

        capability = task.capability
        spec = capability_specs(ctx.settings).get(capability)
        if spec is None or not spec.enabled or spec.effect != task.effect:
            raise DomainError("capability_disabled", "能力未启用或执行类型不匹配。")
        if (
            spec.requires_patient
            and not ctx.scope.patient_id
            or spec.requires_space
            and not ctx.scope.space_id
        ):
            raise DomainError("scope_required", "缺少可信身份或空间。", outcome="clarification")
        try:
            arguments = spec.arguments.model_validate(arguments).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise DomainError("invalid_arguments", "执行参数不符合能力契约。") from exc
        if any(b.source_kind == "evidence_index" for b in task.bindings):
            anchor = ctx.memory.current_record
            evidence = (
                await ctx.repository.evidence(ctx.scope.scope_hash, anchor.evidence_id)
                if anchor
                else None
            )
            if evidence is None or evidence.source_version != anchor.source_version:
                raise DomainError(
                    "anchor_expired", "当前记录来源已过期，请重新定位。", outcome="clarification"
                )

        if capability.startswith("rehab."):
            return await self._rehab.execute(
                task,
                arguments,
                ctx,
            )

        if capability.startswith("scene."):
            return await self._scene.execute(
                task,
                arguments,
                ctx,
            )

        if capability == "doctors.search":
            return await self._doctors.execute(
                task,
                arguments,
                ctx,
            )

        if capability == "knowledge.search":
            return await self._knowledge.execute(task, arguments, ctx)

        if capability == "answer.compose":
            raise DomainError(
                "answer_not_executor_task",
                "回答生成不应进入领域工具执行器。",
            )

        raise DomainError(
            "unknown_capability",
            f"未注册的执行能力：{capability}",
            outcome="unsupported",
        )
