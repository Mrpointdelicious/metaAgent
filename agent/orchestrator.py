from __future__ import annotations

import os
from typing import Any

from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, Runner, set_tracing_disabled

from config import ResolvedLLMConfig, Settings, get_settings
from repositories import RehabRepository
from services import (
    DeviationService,
    ExecutionService,
    GaitService,
    OutcomeService,
    PlanService,
    ReflectionService,
    ReportService,
)
from tools import (
    build_execution_tools,
    build_gait_tools,
    build_outcome_tools,
    build_plan_tools,
    build_reflection_tools,
    build_report_tools,
)

from .instructions import build_task_instructions
from .schemas import OrchestratorRequest, OrchestratorResponse, TaskType


class RehabAgentOrchestrator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

        self.repository = RehabRepository(self.settings)
        self.plan_service = PlanService(self.repository, self.settings)
        self.execution_service = ExecutionService(self.repository, self.settings)
        self.outcome_service = OutcomeService(self.repository, self.settings)
        self.gait_service = GaitService(self.repository, self.settings)
        self.deviation_service = DeviationService(self.settings)
        self.reflection_service = ReflectionService()
        self.report_service = ReportService(
            self.repository,
            self.plan_service,
            self.execution_service,
            self.outcome_service,
            self.gait_service,
            self.deviation_service,
            self.reflection_service,
        )

        self.single_review_tools = (
            build_plan_tools(self.plan_service)
            + build_execution_tools(
                self.plan_service,
                self.execution_service,
                self.outcome_service,
                self.deviation_service,
            )
            + build_outcome_tools(self.plan_service, self.outcome_service)
            + build_gait_tools(self.gait_service)
            + build_report_tools(self.report_service)
            + build_reflection_tools(self.report_service)
        )
        self.group_tools = build_report_tools(self.report_service)

    def classify_request(self, request: OrchestratorRequest) -> TaskType:
        if request.task_type:
            return request.task_type
        text = (request.raw_text or "").lower()
        if request.patient_id is not None or request.plan_id is not None:
            return "single_review"
        if request.therapist_id is not None and ("week" in text or "周报" in text):
            return "weekly_report"
        if request.therapist_id is not None:
            return "risk_screen"
        return "unsupported"

    def run(self, request: OrchestratorRequest) -> OrchestratorResponse:
        task_type = self.classify_request(request)
        llm_config = self.settings.resolve_llm_config(
            provider=request.llm_provider,
            model=request.llm_model,
            base_url=request.llm_base_url,
        )

        if task_type == "unsupported":
            return OrchestratorResponse(
                task_type="unsupported",
                execution_mode="direct",
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output=None,
                final_text="当前仅支持单患者复核、多患者风险筛选和周报生成。",
            )

        requested_agent_sdk = request.use_agent_sdk if request.use_agent_sdk is not None else self.settings.has_default_llm_credentials
        use_agent_sdk = bool(requested_agent_sdk and llm_config.can_use_agents_sdk)
        structured_output = self._run_direct(task_type, request)
        if not use_agent_sdk:
            return OrchestratorResponse(
                task_type=task_type,
                execution_mode="direct",
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output=structured_output,
                final_text=self._render_output(task_type, structured_output),
            )

        try:
            agent = self._build_agent(task_type, llm_config)
            result = Runner.run_sync(agent, self._build_prompt(task_type, request, llm_config), max_turns=12)
            final_text = str(result.final_output)
            execution_mode = "agents_sdk"
        except Exception as exc:  # noqa: BLE001
            final_text = self._render_output(task_type, structured_output)
            final_text += f"\n\n[Agent SDK fallback] {exc}"
            execution_mode = "direct_fallback"
        return OrchestratorResponse(
            task_type=task_type,
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=structured_output,
            final_text=final_text,
        )

    def _build_agent(self, task_type: TaskType, llm_config: ResolvedLLMConfig) -> Agent:
        self._apply_runtime_llm_environment(llm_config)
        set_tracing_disabled(not llm_config.tracing_enabled)

        tools = self.single_review_tools if task_type == "single_review" else self.group_tools
        kwargs: dict[str, Any] = {
            "name": f"rehab_{task_type}_agent",
            "instructions": build_task_instructions(task_type),
            "tools": tools,
        }
        model = self._build_agent_model(llm_config)
        if model is not None:
            kwargs["model"] = model
        return Agent(**kwargs)

    def _build_agent_model(self, llm_config: ResolvedLLMConfig) -> Any | None:
        if llm_config.provider == "openai" and not llm_config.base_url:
            return llm_config.model or None
        if not llm_config.api_key:
            raise ValueError(f"{llm_config.provider} 缺少 API key。")
        if not llm_config.model:
            raise ValueError(f"{llm_config.provider} 缺少模型名。")
        client_kwargs: dict[str, Any] = {"api_key": llm_config.api_key}
        if llm_config.base_url:
            client_kwargs["base_url"] = llm_config.base_url
        client = AsyncOpenAI(**client_kwargs)
        return OpenAIChatCompletionsModel(model=llm_config.model, openai_client=client)

    def _apply_runtime_llm_environment(self, llm_config: ResolvedLLMConfig) -> None:
        if llm_config.provider == "openai" and llm_config.api_key:
            os.environ["OPENAI_API_KEY"] = llm_config.api_key
        elif not self.settings.openai_api_key and "OPENAI_API_KEY" in os.environ:
            del os.environ["OPENAI_API_KEY"]

    def _build_prompt(self, task_type: TaskType, request: OrchestratorRequest, llm_config: ResolvedLLMConfig) -> str:
        return (
            f"任务类型: {task_type}\n"
            f"LLM Provider: {llm_config.provider}\n"
            f"LLM Model: {llm_config.model}\n"
            f"patient_id: {request.patient_id}\n"
            f"plan_id: {request.plan_id}\n"
            f"therapist_id: {request.therapist_id}\n"
            f"days: {request.days}\n"
            f"top_k: {request.top_k}\n"
            f"原始需求: {request.raw_text or 'structured cli request'}"
        )

    def _run_direct(self, task_type: TaskType, request: OrchestratorRequest) -> dict | list:
        if task_type == "single_review":
            return self.report_service.generate_review_card(
                patient_id=request.patient_id,
                plan_id=request.plan_id,
                therapist_id=request.therapist_id,
                days=request.days or self.settings.default_time_window_days,
            ).model_dump(mode="json")
        if task_type == "risk_screen":
            return [
                item.model_dump(mode="json")
                for item in self.report_service.screen_risk_patients(
                    therapist_id=request.therapist_id or 0,
                    days=request.days or self.settings.default_weekly_report_days,
                    top_k=request.top_k,
                )
            ]
        return self.report_service.generate_weekly_risk_report(
            therapist_id=request.therapist_id or 0,
            days=request.days or self.settings.default_weekly_report_days,
            top_k=request.top_k,
        ).model_dump(mode="json")

    def _render_output(self, task_type: TaskType, payload: dict | list) -> str:
        if task_type == "single_review":
            return self._render_review_card(payload)
        if task_type == "risk_screen":
            return self._render_risk_screen(payload)
        return self._render_weekly_report(payload)

    def _render_review_card(self, payload: dict) -> str:
        reflection = payload.get("reflection", {})
        gait = payload.get("gait_explanation", {})
        metrics = payload.get("deviation_metrics", {})
        outcome = payload.get("outcome_change", {})
        focus = "\n".join(f"- {item}" for item in payload.get("review_focus", []))
        interventions = "\n".join(f"- {item}" for item in payload.get("initial_interventions", []))
        return (
            f"单患者复核\n"
            f"患者: {payload.get('patient_id')}\n"
            f"计划: {payload.get('primary_plan_id')}\n"
            f"时间范围: {payload.get('time_range', {}).get('label')}\n"
            f"风险等级: {metrics.get('risk_level')} ({metrics.get('risk_score')})\n"
            f"偏离摘要: {metrics.get('summary_text')}\n"
            f"结果摘要: {outcome.get('summary_text')}\n"
            f"步态补充: {gait.get('note')}\n"
            f"复核重点:\n{focus or '- 无'}\n"
            f"介入建议:\n{interventions or '- 无'}\n"
            f"人工确认: {reflection.get('recommend_manual_confirmation')}\n"
            f"总述: {payload.get('narrative_summary')}"
        )

    def _render_risk_screen(self, payload: list[dict]) -> str:
        lines = ["多患者风险筛选"]
        for index, item in enumerate(payload, start=1):
            lines.append(
                f"{index}. 患者 {item.get('patient_id')} | 风险 {item.get('risk_level')} ({item.get('risk_score')}) | {item.get('summary')}"
            )
        return "\n".join(lines)

    def _render_weekly_report(self, payload: dict) -> str:
        lines = [
            "周报 / 风险摘要",
            f"治疗师: {payload.get('therapist_id')}",
            f"时间范围: {payload.get('time_range', {}).get('label')}",
            f"患者数: {payload.get('patient_count')}",
            f"高风险: {payload.get('high_risk_count')}",
            f"优先患者: {payload.get('priority_patient_ids')}",
            f"摘要: {payload.get('summary_text')}",
        ]
        for item in payload.get("patients", []):
            lines.append(
                f"- 患者 {item.get('patient_id')} | 风险 {item.get('risk_level')} ({item.get('risk_score')}) | {item.get('summary')}"
            )
        return "\n".join(lines)
