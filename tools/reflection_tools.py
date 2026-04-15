from __future__ import annotations

from agents import function_tool

from services import ReportService

from .base import ReflectOnOutputInput, ToolSpec


def build_reflection_tools(report_service: ReportService) -> list[ToolSpec]:
    def _reflect_on_output(
        task_type: str | None = None,
        current_output=None,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        payload = current_output
        issues: list[str] = []
        missing_fields: list[str] = []
        consistency_notes: list[str] = []
        recommend_manual_confirmation = False

        if payload is None and (patient_id is not None or plan_id is not None):
            payload = report_service.generate_review_card(
                patient_id=patient_id,
                plan_id=plan_id,
                therapist_id=therapist_id,
                days=days,
            ).model_dump(mode="json")

        if not payload:
            issues.append("structured_output.empty")

        if isinstance(payload, dict):
            source_backend = payload.get("source_backend")
            if source_backend == "mock":
                consistency_notes.append("当前输出使用的是 mock 数据源；最终表述必须限制在 demo 数据范围内。")

        if task_type == "review_patient" and isinstance(payload, dict):
            review_reflection = payload.get("reflection") or {}
            missing_fields.extend(review_reflection.get("missing_fields", []))
            consistency_notes.extend(review_reflection.get("consistency_notes", []))
            recommend_manual_confirmation = bool(review_reflection.get("recommend_manual_confirmation"))

            gait_block = payload.get("gait_explanation") or {}
            driver_flags = (payload.get("deviation_metrics") or {}).get("driver_flags") or []
            if gait_block and any("gait" in str(flag).lower() or "walk" in str(flag).lower() for flag in driver_flags):
                issues.append("b_chain_evidence_mixed_into_risk_driver_flags")
            if gait_block and gait_block.get("available") and not gait_block.get("note"):
                issues.append("gait_evidence_missing_scope_note")

        if task_type in {"screen_risk", "weekly_report"} and isinstance(payload, dict):
            if "patients" not in payload:
                issues.append("patients.missing")
            if "gait_explanation" in payload:
                issues.append("b_chain_block_should_not_exist_in_group_output")

        evidence_sufficient = not missing_fields and "structured_output.empty" not in issues
        if issues:
            recommend_manual_confirmation = True

        summary_parts: list[str] = []
        if evidence_sufficient:
            summary_parts.append("证据基本充分")
        else:
            summary_parts.append("证据链不完整")
        if recommend_manual_confirmation:
            summary_parts.append("建议人工确认")
        if issues:
            summary_parts.append(f"问题数={len(issues)}")

        return {
            "task_type": task_type or "unknown",
            "evidence_sufficient": evidence_sufficient,
            "missing_fields": missing_fields,
            "consistency_notes": consistency_notes,
            "issues": issues,
            "recommend_manual_confirmation": recommend_manual_confirmation,
            "summary_text": "; ".join(summary_parts),
        }

    @function_tool
    def reflect_on_output(
        task_type: str | None = None,
        current_output=None,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """执行受约束的输出检查，不新增业务事实，也不修改风险评分。"""
        return _reflect_on_output(
            task_type=task_type,
            current_output=current_output,
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )

    return [
        ToolSpec(
            tool_name="reflect_on_output",
            description="对当前结构化输出执行受约束的护栏检查。",
            input_model=ReflectOnOutputInput,
            output_schema="包含问题项、缺失字段和人工确认建议的护栏检查结果 JSON。",
            chain_scope="cross",
            can_affect_risk_score=False,
            direct_handler=_reflect_on_output,
            agent_tool=reflect_on_output,
            agent_handler=_reflect_on_output,
        )
    ]
