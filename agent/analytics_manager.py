from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings
from models import PatientSet, RankedPatients
from services import AnalyticsService
from services.shared import build_time_range
from tools import ToolSpec

from .schemas import (
    AnalyticsStructuredOutput,
    IntentDecision,
    OrchestratorRequest,
    OrchestratorResponse,
    QueryPlan,
    QueryPlanStep,
    StepExecutionResult,
)


class AnalyticsManager:
    def __init__(
        self,
        analytics_service: AnalyticsService,
        analytics_tool_registry: dict[str, ToolSpec],
        settings: Settings | None = None,
    ):
        self.analytics_service = analytics_service
        self.analytics_tool_registry = analytics_tool_registry
        self.settings = settings or get_settings()

    def run(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        doctor_id = request.therapist_id or self._extract_doctor_id(question) or self.settings.demo_default_therapist_id
        days = request.days or self._extract_days(question) or self.settings.default_time_window_days
        time_range = build_time_range(
            self.analytics_service.repository,
            therapist_id=doctor_id,
            days=days,
        )
        recent_start = time_range.start.date().isoformat()
        recent_end = time_range.end.date().isoformat()
        historical_end = (time_range.start - timedelta(seconds=1)).date().isoformat()
        top_k = request.top_k or 20

        query_plan = QueryPlan(
            normalized_question=f"查看医生 {doctor_id} 在最近 {days} 天内，以前到训过但最近未到训的患者。",
            doctor_id=doctor_id,
            start_date=recent_start,
            end_date=recent_end,
            steps=[
                QueryPlanStep(
                    step_id="step_1",
                    intent="historical_seen_cohort",
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": doctor_id, "start_date": None, "end_date": historical_end, "source": "attendance"},
                    rationale="先找出最近窗口开始之前曾实际到训过的患者集合。",
                ),
                QueryPlanStep(
                    step_id="step_2",
                    intent="recent_seen_cohort",
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": doctor_id, "start_date": recent_start, "end_date": recent_end, "source": "attendance"},
                    rationale="再找出最近窗口内实际到训过的患者集合。",
                ),
                QueryPlanStep(
                    step_id="step_3",
                    intent="recent_active_plans",
                    tool_name="list_patients_with_active_plans",
                    arguments={"doctor_id": doctor_id, "start_date": recent_start, "end_date": recent_end},
                    rationale="补充最近窗口内仍有计划的患者集合，用于后续优先级判断。",
                ),
                QueryPlanStep(
                    step_id="step_4",
                    intent="absent_candidate_diff",
                    tool_name="set_diff",
                    arguments={},
                    rationale="对历史到训集合减去最近到训集合，得到‘以前来过但最近没来’的患者。",
                ),
                QueryPlanStep(
                    step_id="step_5",
                    intent="last_visit_enrichment",
                    tool_name="get_patient_last_visit",
                    arguments={"doctor_id": doctor_id},
                    rationale="为候选患者补充最近一次到训时间。",
                ),
                QueryPlanStep(
                    step_id="step_6",
                    intent="plan_status_enrichment",
                    tool_name="get_patient_plan_status",
                    arguments={"doctor_id": doctor_id, "start_date": recent_start, "end_date": recent_end},
                    rationale="补充最近窗口内是否仍有计划以及计划/到训状态。",
                ),
                QueryPlanStep(
                    step_id="step_7",
                    intent="ranking",
                    tool_name="rank_patients",
                    arguments={"strategy": "active_plan_but_absent", "top_k": top_k},
                    rationale="优先把‘有计划但未到训’的患者排在前面。",
                ),
            ],
        )

        step_results: list[StepExecutionResult] = []
        validation_issues: list[str] = []

        historical_seen = self._execute_step(
            step_results=step_results,
            step_id="step_1",
            tool_name="list_patients_seen_by_doctor",
            args=query_plan.steps[0].arguments,
            mode=mode,
        )
        recent_seen = self._execute_step(
            step_results=step_results,
            step_id="step_2",
            tool_name="list_patients_seen_by_doctor",
            args=query_plan.steps[1].arguments,
            mode=mode,
        )
        active_plans = self._execute_step(
            step_results=step_results,
            step_id="step_3",
            tool_name="list_patients_with_active_plans",
            args=query_plan.steps[2].arguments,
            mode=mode,
        )
        absent_candidates = self._execute_step(
            step_results=step_results,
            step_id="step_4",
            tool_name="set_diff",
            args={
                "base_set_id": historical_seen.get("set_id") if isinstance(historical_seen, dict) else "",
                "subtract_set_id": recent_seen.get("set_id") if isinstance(recent_seen, dict) else "",
            },
            mode=mode,
        )

        absent_patient_ids = (absent_candidates or {}).get("patient_ids") or []
        if not absent_patient_ids:
            summary = (
                f"医生 {doctor_id} 在 {recent_start} 至 {recent_end} 的最近 {days} 天内，"
                "未发现“以前来过但最近没来”的患者。"
            )
            structured_output = AnalyticsStructuredOutput(
                question=question,
                doctor_id=doctor_id,
                time_range=time_range,
                source_backend=self.analytics_service.repository.last_backend,
                query_plan=query_plan,
                historical_seen_set=PatientSet.model_validate(historical_seen) if isinstance(historical_seen, dict) else None,
                recent_seen_set=PatientSet.model_validate(recent_seen) if isinstance(recent_seen, dict) else None,
                absent_set=PatientSet.model_validate(absent_candidates) if isinstance(absent_candidates, dict) else None,
                ranked_patients=None,
                result_rows=[],
                evidence_basis=self._build_evidence_basis(),
                summary=summary,
            )
            final_text = self._render_final_text(structured_output, explicit_doctor=request.therapist_id is not None)
            validation_issues.extend(self._validate(structured_output, final_text))
            return OrchestratorResponse(
                success=True,
                task_type="open_analytics_query",
                execution_mode=execution_mode,
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output=structured_output.model_dump(mode="json"),
                final_text=final_text,
                validation_issues=validation_issues,
                execution_trace=step_results,
            )

        for patient_id in absent_patient_ids:
            self._execute_step(
                step_results=step_results,
                step_id=f"step_5_patient_{patient_id}",
                tool_name="get_patient_last_visit",
                args={"patient_id": patient_id, "doctor_id": doctor_id},
                mode=mode,
            )
        for patient_id in absent_patient_ids:
            self._execute_step(
                step_results=step_results,
                step_id=f"step_6_patient_{patient_id}",
                tool_name="get_patient_plan_status",
                args={
                    "patient_id": patient_id,
                    "doctor_id": doctor_id,
                    "start_date": recent_start,
                    "end_date": recent_end,
                },
                mode=mode,
            )
        ranked_payload = self._execute_step(
            step_results=step_results,
            step_id="step_7",
            tool_name="rank_patients",
            args={"patient_ids": absent_patient_ids, "strategy": "active_plan_but_absent", "top_k": top_k},
            mode=mode,
        )
        ranked_patients = (
            RankedPatients.model_validate(ranked_payload)
            if isinstance(ranked_payload, dict)
            else self.analytics_service.rank_patients(
                patient_ids=absent_patient_ids,
                strategy="active_plan_but_absent",
                top_k=top_k,
            )
        )
        result_rows = self.analytics_service.build_result_rows(ranked_patients)
        active_plan_absent_count = sum(1 for row in result_rows if row.has_active_plan_in_window)
        shown_count = len(result_rows)
        total_absent_count = len(absent_patient_ids)
        active_plan_set = PatientSet.model_validate(active_plans) if isinstance(active_plans, dict) else None
        summary = (
            f"医生 {doctor_id} 在 {recent_start} 至 {recent_end} 的最近 {days} 天内，"
            f"共有 {total_absent_count} 名患者属于“以前来过但最近未到训”。"
            f"其中 {active_plan_absent_count} 名患者在最近窗口内仍有计划。"
            + (f" 当前展示前 {shown_count} 名。" if shown_count < total_absent_count else "")
        )
        structured_output = AnalyticsStructuredOutput(
            question=question,
            doctor_id=doctor_id,
            time_range=time_range,
            source_backend=self.analytics_service.repository.last_backend,
            query_plan=query_plan,
            historical_seen_set=PatientSet.model_validate(historical_seen) if isinstance(historical_seen, dict) else None,
            recent_seen_set=PatientSet.model_validate(recent_seen) if isinstance(recent_seen, dict) else None,
            absent_set=PatientSet.model_validate(absent_candidates) if isinstance(absent_candidates, dict) else None,
            ranked_patients=ranked_patients,
            result_rows=result_rows,
            evidence_basis=self._build_evidence_basis(active_plan_set=active_plan_set),
            summary=summary,
        )
        final_text = self._render_final_text(structured_output, explicit_doctor=request.therapist_id is not None)
        validation_issues.extend(self._validate(structured_output, final_text))
        return OrchestratorResponse(
            success=True,
            task_type="open_analytics_query",
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=structured_output.model_dump(mode="json"),
            final_text=final_text,
            validation_issues=validation_issues,
            execution_trace=step_results,
        )

    def _execute_step(
        self,
        *,
        step_results: list[StepExecutionResult],
        step_id: str,
        tool_name: str,
        args: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        tool = self.analytics_tool_registry.get(tool_name)
        if tool is None:
            raise ValueError(f"tool_not_allowed:{tool_name}")
        try:
            raw_output = tool.invoke(mode=mode, args=args)
            summary = self._summarize_output(tool_name, raw_output)
            step_results.append(
                StepExecutionResult(
                    step_id=step_id,
                    tool_name=tool_name,
                    success=True,
                    args=args,
                    output_summary=summary,
                    raw_output=raw_output,
                )
            )
            return raw_output if isinstance(raw_output, dict) else {}
        except Exception as exc:  # noqa: BLE001
            step_results.append(
                StepExecutionResult(
                    step_id=step_id,
                    tool_name=tool_name,
                    success=False,
                    args=args,
                    output_summary="步骤执行失败",
                    raw_output=None,
                    error=str(exc),
                )
            )
            return {}

    def _summarize_output(self, tool_name: str, payload: dict[str, Any]) -> str:
        if tool_name in {"list_patients_seen_by_doctor", "list_patients_with_active_plans", "set_diff"}:
            return f"返回患者集合，人数={payload.get('count', 0)}"
        if tool_name == "get_patient_last_visit":
            return f"最近到训时间={payload.get('last_visit_time') or '无'}"
        if tool_name == "get_patient_plan_status":
            return (
                f"有计划={payload.get('has_active_plan')}，"
                f"计划数={payload.get('planned_sessions')}，到训数={payload.get('attended_sessions')}"
            )
        if tool_name == "rank_patients":
            return f"排序完成，返回 {len(payload.get('rows') or [])} 名患者"
        return "工具执行完成"

    def _render_final_text(self, output: AnalyticsStructuredOutput, *, explicit_doctor: bool) -> str:
        lines = [
            "开放式分析结果",
            f"问题: {output.question}",
            f"医生: {output.doctor_id}",
            f"时间范围: {output.time_range.label.replace(' to ', ' 至 ') if output.time_range else '未知'}",
            f"摘要: {output.summary}",
        ]
        if not explicit_doctor:
            lines.append(f"说明: 未显式提供医生 ID，本次使用稳定 Demo 医生 {output.doctor_id}。")
        if output.result_rows:
            lines.append("患者列表:")
            for index, row in enumerate(output.result_rows, start=1):
                lines.append(
                    f"{index}. 患者{row.patient_id} | 最近到训 {row.last_visit_time or '无'} | "
                    f"最近窗口有计划 {self._localize_bool(row.has_active_plan_in_window)} | "
                    f"计划/到训 {row.planned_sessions or 0}/{row.attended_sessions or 0} | "
                    f"{row.rank_reason or '未说明'}"
                )
        else:
            lines.append("患者列表: 当前为空。")
        lines.append("数据依据:")
        for item in output.evidence_basis:
            lines.append(f"- {item}")
        return "\n".join(lines)

    def _validate(self, output: AnalyticsStructuredOutput, final_text: str) -> list[str]:
        issues: list[str] = []
        if output.absent_set is None:
            issues.append("analytics.absent_set.missing")
        if output.source_backend == "mock" and "真实数据库" in final_text:
            issues.append("analytics.mock_backend_misreported")
        if "根据数据库推测" in final_text:
            issues.append("analytics.unreliable_phrase")
        if output.ranked_patients and len(output.result_rows) != len(output.ranked_patients.rows):
            issues.append("analytics.result_rows_count_mismatch")
        return issues

    def _build_evidence_basis(self, *, active_plan_set: PatientSet | None = None) -> list[str]:
        items = [
            "历史/最近到训集合来自 dbdevicelog.StartTime 与 dbrehaplan.DoctorId/UserId 的只读汇总。",
            "窗口内计划状态来自 dbrehaplan 的计划记录与窗口内关联执行日志摘要。",
        ]
        if active_plan_set is not None:
            items.append(f"最近窗口内活跃计划患者集合人数={active_plan_set.count}。")
        return items

    def _extract_doctor_id(self, text: str) -> int | None:
        match = re.search(r"(?:医生|治疗师|康复师|doctor|therapist)\s*(?:id)?\s*[:：]?\s*(\d+)", text, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    def _extract_days(self, text: str) -> int | None:
        lowered = text.lower()
        if "本周" in text or "最近一周" in text or "近一周" in text or "last week" in lowered:
            return 7
        if "本月" in text or "最近一个月" in text or "近一个月" in text or "last month" in lowered:
            return 30
        for pattern in (r"(?:最近|过去|近|这)\s*(\d+)\s*天", r"last\s*(\d+)\s*days?", r"(\d+)\s*天"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _localize_bool(self, value: bool | None) -> str:
        if value is True:
            return "是"
        if value is False:
            return "否"
        return "未知"
