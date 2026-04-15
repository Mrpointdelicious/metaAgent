from __future__ import annotations

from agents import function_tool

from services import ReportService

from .base import GenerateReviewCardInput, TherapistWindowInput, ToolSpec


def build_report_tools(report_service: ReportService) -> list[ToolSpec]:
    def _generate_review_card(
        patient_id: int | None = None,
        patient_ids: list[int] | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict | list[dict]:
        if patient_ids:
            review_cards: list[dict] = []
            for target_patient_id in patient_ids:
                review_cards.append(
                    report_service.generate_review_card(
                        patient_id=target_patient_id,
                        therapist_id=therapist_id,
                        days=days,
                    ).model_dump(mode="json")
                )
            return review_cards
        return report_service.generate_review_card(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        ).model_dump(mode="json")

    @function_tool
    def generate_review_card(
        patient_id: int | None = None,
        patient_ids: list[int] | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict | list[dict]:
        """生成面向治疗师的单患者复核卡。风险评分仅来自 A 链。"""
        return _generate_review_card(
            patient_id=patient_id,
            patient_ids=patient_ids,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )

    def _screen_risk_patients(
        therapist_id: int,
        days: int = 7,
        top_k: int = 10,
    ) -> dict:
        weekly_report = report_service.generate_weekly_risk_report(
            therapist_id=therapist_id,
            days=days,
            top_k=top_k,
        )
        return {
            "therapist_id": weekly_report.therapist_id,
            "time_range": weekly_report.time_range.model_dump(mode="json"),
            "source_backend": weekly_report.source_backend,
            "patient_count": weekly_report.patient_count,
            "selected_count": len(weekly_report.patients),
            "patients": [item.model_dump(mode="json") for item in weekly_report.patients],
            "priority_patient_ids": weekly_report.priority_patient_ids,
            "review_card_summaries": [],
            "summary_text": weekly_report.summary_text,
        }

    @function_tool
    def screen_risk_patients(
        therapist_id: int,
        days: int = 7,
        top_k: int = 10,
    ) -> dict:
        """按风险筛选 A 链患者，并返回面向治疗师的排序结果。"""
        return _screen_risk_patients(
            therapist_id=therapist_id,
            days=days,
            top_k=top_k,
        )

    def _generate_weekly_risk_report(
        therapist_id: int,
        days: int = 7,
        top_k: int = 10,
    ) -> dict:
        return report_service.generate_weekly_risk_report(
            therapist_id=therapist_id,
            days=days,
            top_k=top_k,
        ).model_dump(mode="json")

    @function_tool
    def generate_weekly_risk_report(
        therapist_id: int,
        days: int = 7,
        top_k: int = 10,
    ) -> dict:
        """生成治疗师视角的 A 链周报。B 链证据不参与风险评分。"""
        return _generate_weekly_risk_report(
            therapist_id=therapist_id,
            days=days,
            top_k=top_k,
        )

    return [
        ToolSpec(
            tool_name="generate_review_card",
            description="为一个或多个 A 链患者生成结构化复核卡。",
            input_model=GenerateReviewCardInput,
            output_schema="ReviewCard JSON，或由多个 ReviewCard JSON 组成的列表。",
            chain_scope="cross",
            can_affect_risk_score=True,
            direct_handler=_generate_review_card,
            agent_tool=generate_review_card,
            agent_handler=_generate_review_card,
        ),
        ToolSpec(
            tool_name="screen_risk_patients",
            description="按偏离风险对 A 链患者排序，供治疗师筛选复核对象。",
            input_model=TherapistWindowInput,
            output_schema="包含患者列表、优先复核 ID 和摘要文本的风险筛选结果。",
            chain_scope="A",
            can_affect_risk_score=True,
            direct_handler=_screen_risk_patients,
            agent_tool=screen_risk_patients,
            agent_handler=_screen_risk_patients,
        ),
        ToolSpec(
            tool_name="generate_weekly_risk_report",
            description="基于 A 链复核结果生成治疗师周报。",
            input_model=TherapistWindowInput,
            output_schema="WeeklyRiskReport JSON。",
            chain_scope="A",
            can_affect_risk_score=True,
            direct_handler=_generate_weekly_risk_report,
            agent_tool=generate_weekly_risk_report,
            agent_handler=_generate_weekly_risk_report,
        ),
    ]
