from __future__ import annotations

from agents import function_tool

from services import DeviationService, ExecutionService, OutcomeService, PlanService

from .base import PatientPlanWindowInput, ToolSpec


def build_execution_tools(
    plan_service: PlanService,
    execution_service: ExecutionService,
    outcome_service: OutcomeService,
    deviation_service: DeviationService,
) -> list[ToolSpec]:
    def _get_execution_logs(
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
        return execution_service.get_execution_logs(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        ).model_dump(mode="json")

    @function_tool
    def get_execution_logs(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """返回 A 链执行日志和聚合后的 session 证据，供诊断下钻使用。"""
        return _get_execution_logs(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )

    def _calc_deviation_metrics(
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
        execution_summary = execution_service.get_execution_logs(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        )
        outcome_change = outcome_service.get_outcome_change(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        )
        return deviation_service.calc_deviation_metrics(
            plan_summary=plan_summary,
            execution_summary=execution_summary,
            outcome_change=outcome_change,
        ).model_dump(mode="json")

    @function_tool
    def calc_deviation_metrics(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """计算 A 链到训率、完成率、剂量偏差和连续中断风险。"""
        return _calc_deviation_metrics(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )

    return [
        ToolSpec(
            tool_name="get_execution_logs",
            description="获取 A 链执行证据。适合诊断分析，不是默认复核主链。",
            input_model=PatientPlanWindowInput,
            output_schema="ExecutionSummary JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_execution_logs,
            agent_tool=get_execution_logs,
            agent_handler=_get_execution_logs,
        ),
        ToolSpec(
            tool_name="calc_deviation_metrics",
            description="基于 service 层证据计算 A 链偏离指标和风险分。",
            input_model=PatientPlanWindowInput,
            output_schema="DeviationMetrics JSON。",
            chain_scope="A",
            can_affect_risk_score=True,
            direct_handler=_calc_deviation_metrics,
            agent_tool=calc_deviation_metrics,
            agent_handler=_calc_deviation_metrics,
        ),
    ]
