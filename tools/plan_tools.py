from __future__ import annotations

from agents import function_tool

from services import PlanService

from .base import PatientPlanWindowInput, ToolSpec


def build_plan_tools(plan_service: PlanService) -> list[ToolSpec]:
    def _get_plan_summary(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        return plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        ).model_dump(mode="json")

    @function_tool
    def get_plan_summary(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """返回指定时间窗口内的 A 链计划层摘要。"""
        return _get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )

    return [
        ToolSpec(
            tool_name="get_plan_summary",
            description="获取 A 链计划摘要。适合诊断下钻，不是默认主链工具。",
            input_model=PatientPlanWindowInput,
            output_schema="PlanSummary JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_plan_summary,
            agent_tool=get_plan_summary,
            agent_handler=_get_plan_summary,
        )
    ]
