from __future__ import annotations

from agents import function_tool

from services import OutcomeService, PlanService

from .base import PatientPlanWindowInput, ToolSpec


def build_outcome_tools(plan_service: PlanService, outcome_service: OutcomeService) -> list[ToolSpec]:
    def _get_outcome_change(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        plan_summary = plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )
        return outcome_service.get_outcome_change(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        ).model_dump(mode="json")

    @function_tool
    def get_outcome_change(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """返回 A 链结果变化摘要，例如 HealthScore 和 GameScore 趋势。"""
        return _get_outcome_change(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )

    return [
        ToolSpec(
            tool_name="get_outcome_change",
            description="获取 A 链结果趋势证据，供诊断查看。",
            input_model=PatientPlanWindowInput,
            output_schema="OutcomeChangeSummary JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_outcome_change,
            agent_tool=get_outcome_change,
            agent_handler=_get_outcome_change,
        )
    ]
