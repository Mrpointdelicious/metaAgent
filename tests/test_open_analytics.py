from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import ResolvedLLMConfig, Settings
from repositories import RehabRepository
from repositories.db_client import DatabaseConnectionError
from services import AnalyticsService
from tools import build_analytics_tools, build_tool_registry

from agent.analytics_manager import AnalyticsManager
from agent.intent_router import IntentRouter
from agent.llm_router import LLMRouter, merge_rule_and_llm
from agent.orchestrator import RehabAgentOrchestrator
from agent.plan_validator import PlanValidator
from agent.schemas import (
    AgentAnalyticsResult,
    AgentToolCallRecord,
    ExecutionStrategy,
    IntentDecision,
    LLMPlannedQuery,
    LLMPlannedStep,
    OrchestrationTaskType,
    OrchestratorRequest,
    RoutedDecision,
)

CASE_1_QUESTION = "查看医生56这30天有哪些以前来过的患者没有来"
CASE_2_QUESTION = "看医生56这30天有哪些是前80-30天以前来过的患者，这30没有来"
CASE_3_QUESTION = "查询一下这30天哪些医生有定患者训练计划？"


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for tests")


def _build_manager() -> AnalyticsManager:
    settings = Settings(use_mock_when_db_unavailable=True)
    repository = RehabRepository(settings)
    repository.client.query = _raise_db_error  # type: ignore[method-assign]
    analytics_service = AnalyticsService(repository, settings)
    analytics_tools = build_analytics_tools(analytics_service)
    return AnalyticsManager(
        analytics_service=analytics_service,
        analytics_tool_registry=build_tool_registry(analytics_tools),
        settings=settings,
    )


class UnavailableFakeAgentRuntime:
    def can_run(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return False

    def run(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        raise AssertionError("agent runtime should not run")


class SuccessfulFakeAgentRuntime:
    def __init__(self) -> None:
        self.seen_tool_names: list[str] = []

    def can_run(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return True

    def run(self, **kwargs):  # noqa: ANN003, ANN201
        self.seen_tool_names = [tool.tool_name for tool in kwargs["tool_specs"]]
        return AgentAnalyticsResult(
            normalized_question="Agent runtime handled the dual-window question.",
            subtype="absent_from_baseline_window",
            scope="single_doctor",
            final_text="Agent runtime result.",
            structured_output={
                "summary": "Agent runtime result.",
                "result_rows": [{"patient_id": 20001, "rank": 1}],
                "fallback_required": False,
            },
            tool_calls=[
                AgentToolCallRecord(
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": 56, "start_date": "2024-12-10", "end_date": "2025-01-28"},
                    output_summary="patient_count=5",
                )
            ],
            rationale="Used fake agent runtime for unit coverage.",
        )


class FailingFakeAgentRuntime:
    def can_run(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return True

    def run(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        raise RuntimeError("forced agent runtime failure")


class ExplodingFakePlanner:
    def plan(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        raise AssertionError("planner should not run")


def _build_manager_with_planner(fake_planner, agent_runtime=None) -> AnalyticsManager:  # noqa: ANN001
    settings = Settings(use_mock_when_db_unavailable=True)
    repository = RehabRepository(settings)
    repository.client.query = _raise_db_error  # type: ignore[method-assign]
    analytics_service = AnalyticsService(repository, settings)
    analytics_tools = build_analytics_tools(analytics_service)
    registry = build_tool_registry(analytics_tools)
    return AnalyticsManager(
        analytics_service=analytics_service,
        analytics_tool_registry=registry,
        settings=settings,
        llm_planner=fake_planner,
        plan_validator=PlanValidator(registry),
        agent_runtime=agent_runtime or UnavailableFakeAgentRuntime(),
    )


def _build_orchestrator() -> RehabAgentOrchestrator:
    return RehabAgentOrchestrator(Settings(use_mock_when_db_unavailable=True))


def _llm_config() -> ResolvedLLMConfig:
    return ResolvedLLMConfig(provider="qwen", model="test-model")


def _agent_llm_config() -> ResolvedLLMConfig:
    return ResolvedLLMConfig(provider="qwen", api_key="test-key", model="test-model", base_url="http://localhost")


def _template_strategy(confidence: float | None = None) -> ExecutionStrategy:
    return ExecutionStrategy(kind="template_analytics", reason="unit test template analytics", confidence=confidence)


def _agent_strategy(confidence: float | None = None) -> ExecutionStrategy:
    return ExecutionStrategy(kind="agent_planned", reason="unit test agent planned", confidence=confidence)


def _patient_name_routed_decision() -> RoutedDecision:
    rule = IntentDecision(
        intent="open_analytics_query",
        confidence=0.9,
        rationale="unit test patient name display",
        analytics_subtype="absent_old_patients_recent_window",
        analysis_scope="single_doctor",
        doctor_id_source="session",
    )
    return RoutedDecision(
        rule_decision=rule,
        final_intent="open_analytics_query",
        final_subtype="absent_old_patients_recent_window",
        final_scope="single_doctor",
        doctor_id_source="session",
        confidence=0.9,
        rationale="unit test patient name display",
    )


class ValidFakePlanner:
    def plan(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return LLMPlannedQuery(
            normalized_question="Compare baseline and recent attendance.",
            subtype="absent_from_baseline_window",
            scope="single_doctor",
            steps=[
                LLMPlannedStep(
                    step_id="baseline_seen",
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": 56, "start_date": "BASELINE_START", "end_date": "BASELINE_END", "source": "attendance"},
                    rationale="Collect baseline patients.",
                ),
                LLMPlannedStep(
                    step_id="recent_seen",
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": 56, "start_date": "RECENT_START", "end_date": "RECENT_END", "source": "attendance"},
                    rationale="Collect recent patients.",
                ),
                LLMPlannedStep(
                    step_id="absent_diff",
                    tool_name="set_diff",
                    arguments={"base_set_ref": "baseline_seen", "subtract_set_ref": "recent_seen"},
                    rationale="Subtract recent patients from baseline.",
                ),
            ],
            confidence=0.9,
            rationale="Dual-window question needs a set comparison.",
        )


class InvalidToolFakePlanner:
    def plan(self, **kwargs):  # noqa: ANN003, ANN201
        del kwargs
        return LLMPlannedQuery(
            normalized_question="Invalid tool simulation.",
            subtype="absent_from_baseline_window",
            scope="single_doctor",
            steps=[
                LLMPlannedStep(
                    step_id="bad_tool",
                    tool_name="not_a_real_tool",
                    arguments={},
                    rationale="Simulate a hallucinated primitive.",
                )
            ],
            confidence=0.2,
            rationale="Test validator interception.",
        )


class OpenAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = IntentRouter()
        self.manager = _build_manager()

    def test_single_window_absent(self) -> None:
        question = CASE_1_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)

        self.assertEqual(decision.intent, "open_analytics_query")
        self.assertEqual(decision.analytics_subtype, "absent_old_patients_recent_window")

        response = self.manager.run(
            request=request,
            routed_decision=merge_rule_and_llm(decision, None),
            strategy=_template_strategy(decision.confidence),
            mode="direct",
            llm_config=_llm_config(),
            execution_mode="direct",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["subtype"], "absent_old_patients_recent_window")
        self.assertEqual(payload["doctor_id"], 56)
        self.assertIsNotNone(payload["query_plan"]["recent_start_date"])
        self.assertIsNone(payload["query_plan"]["baseline_start_date"])

        routed = merge_rule_and_llm(decision, None)
        self.assertEqual(routed.final_intent, "open_analytics_query")
        self.assertEqual(routed.final_subtype, "absent_old_patients_recent_window")
        self.assertEqual(routed.final_scope, "single_doctor")
        self.assertEqual(routed.doctor_id_source, "explicit")

    def test_dual_window_absent(self) -> None:
        question = CASE_2_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)

        self.assertEqual(decision.analytics_subtype, "absent_from_baseline_window")

        response = self.manager.run(
            request=request,
            routed_decision=merge_rule_and_llm(decision, None),
            strategy=_template_strategy(decision.confidence),
            mode="direct",
            llm_config=_llm_config(),
            execution_mode="direct",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["subtype"], "absent_from_baseline_window")
        self.assertEqual(payload["query_plan"]["time_parse_mode"], "dual_window")
        self.assertIsNotNone(payload["query_plan"]["recent_start_date"])
        self.assertIsNotNone(payload["query_plan"]["baseline_start_date"])
        self.assertIsNotNone(payload["query_plan"]["baseline_end_date"])

        routed = merge_rule_and_llm(decision, None)
        self.assertEqual(routed.final_intent, "open_analytics_query")
        self.assertEqual(routed.final_subtype, "absent_from_baseline_window")
        self.assertEqual(routed.final_scope, "single_doctor")
        self.assertEqual(routed.doctor_id_source, "explicit")

    def test_patient_result_rows_display_enriched_names(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            therapist_id=30001,
            days=1,
            raw_text="unit test patient name display",
        )
        response = self.manager.run(
            request=request,
            routed_decision=_patient_name_routed_decision(),
            strategy=_template_strategy(0.9),
            mode="direct",
            llm_config=_llm_config(),
            execution_mode="direct",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertTrue(payload["result_rows"])
        self.assertEqual(payload["result_rows"][0]["patient_name"], "Mock Patient Alpha")
        self.assertEqual(payload["result_rows"][0]["doctor_name"], "Mock Doctor One")
        self.assertIn("Mock Patient Alpha（患者20001）", response.final_text)
        self.assertIn("Mock Doctor One（医生30001）", response.final_text)

    def test_missing_user_names_fall_back_to_id_labels(self) -> None:
        manager = _build_manager()
        manager.analytics_service.repository.get_user_name_map = lambda user_ids: {}  # type: ignore[method-assign]
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            therapist_id=30001,
            days=1,
            raw_text="unit test missing user names",
        )

        response = manager.run(
            request=request,
            routed_decision=_patient_name_routed_decision(),
            strategy=_template_strategy(0.9),
            mode="direct",
            llm_config=_llm_config(),
            execution_mode="direct",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertTrue(payload["result_rows"])
        self.assertIsNone(payload["result_rows"][0]["patient_name"])
        self.assertIn("患者20001", response.final_text)
        self.assertIn("医生30001", response.final_text)

    def test_doctor_aggregate(self) -> None:
        question = CASE_3_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            therapist_id=56,
            raw_text=question,
        )
        decision = self.router.route(request)

        self.assertEqual(decision.analytics_subtype, "doctors_with_active_plans")
        self.assertEqual(decision.analysis_scope, "doctor_aggregate")

        response = self.manager.run(
            request=request,
            routed_decision=merge_rule_and_llm(decision, None),
            strategy=_template_strategy(decision.confidence),
            mode="direct",
            llm_config=_llm_config(),
            execution_mode="direct",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["analysis_scope"], "doctor_aggregate")
        self.assertIsNone(payload["doctor_id"])
        self.assertTrue(payload["result_rows"])
        self.assertEqual(payload["result_rows"][0]["doctor_id"], 30001)
        self.assertEqual(payload["result_rows"][0]["doctor_name"], "Mock Doctor One")
        self.assertIn("Mock Doctor One（医生30001）", response.final_text)

        routed = merge_rule_and_llm(decision, None)
        self.assertEqual(routed.final_intent, "open_analytics_query")
        self.assertEqual(routed.final_subtype, "doctors_with_active_plans")
        self.assertEqual(routed.final_scope, "doctor_aggregate")
        self.assertEqual(routed.doctor_id_source, "none")

    def test_agent_planned_dual_window_uses_llm_plan(self) -> None:
        question = CASE_2_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        manager = _build_manager_with_planner(ValidFakePlanner())

        response = manager.run(
            request=request,
            routed_decision=routed,
            strategy=_agent_strategy(routed.confidence),
            mode="agents_sdk",
            llm_config=_agent_llm_config(),
            execution_mode="agents_sdk",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["planned_query_source"]["source"], "llm_planner")
        self.assertEqual(payload["query_plan"]["subtype"], "absent_from_baseline_window")
        self.assertEqual([step["step_id"] for step in payload["query_plan"]["steps"]], ["baseline_seen", "recent_seen", "absent_diff"])
        self.assertTrue(any(item.tool_name == "plan_validator" and item.success for item in response.execution_trace))

    def test_agent_planned_uses_agent_runtime_before_planner(self) -> None:
        question = CASE_2_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        runtime = SuccessfulFakeAgentRuntime()
        manager = _build_manager_with_planner(ExplodingFakePlanner(), agent_runtime=runtime)

        response = manager.run(
            request=request,
            routed_decision=routed,
            strategy=_agent_strategy(routed.confidence),
            mode="agents_sdk",
            llm_config=_agent_llm_config(),
            execution_mode="agents_sdk",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["planned_query_source"]["source"], "agents_sdk_runtime")
        self.assertEqual(payload["result_rows"][0]["patient_name"], "Mock Patient Alpha")
        self.assertEqual(payload["agent_runtime"]["tool_call_count"], 1)
        self.assertIn("Mock Patient Alpha（患者20001）", response.final_text)
        self.assertIn("list_patients_seen_by_doctor", runtime.seen_tool_names)
        self.assertTrue(any(item.tool_name == "open_analytics_agent" and item.success for item in response.execution_trace))
        self.assertFalse(any(item.tool_name == "plan_validator" for item in response.execution_trace))

    def test_agent_runtime_failure_falls_back_to_llm_planner(self) -> None:
        question = CASE_2_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        manager = _build_manager_with_planner(ValidFakePlanner(), agent_runtime=FailingFakeAgentRuntime())

        response = manager.run(
            request=request,
            routed_decision=routed,
            strategy=_agent_strategy(routed.confidence),
            mode="agents_sdk",
            llm_config=_agent_llm_config(),
            execution_mode="agents_sdk",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["planned_query_source"]["source"], "llm_planner")
        self.assertTrue(any(issue.startswith("agents_sdk_runtime.fallback:") for issue in response.validation_issues))
        self.assertTrue(any(item.tool_name == "open_analytics_agent" and not item.success for item in response.execution_trace))
        self.assertTrue(any(item.tool_name == "plan_validator" and item.success for item in response.execution_trace))

    def test_agent_planned_invalid_tool_falls_back_to_template(self) -> None:
        question = CASE_2_QUESTION
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        manager = _build_manager_with_planner(InvalidToolFakePlanner())

        response = manager.run(
            request=request,
            routed_decision=routed,
            strategy=_agent_strategy(routed.confidence),
            mode="agents_sdk",
            llm_config=_agent_llm_config(),
            execution_mode="agents_sdk",
        )
        payload = response.structured_output

        self.assertTrue(response.success)
        self.assertEqual(payload["planned_query_source"]["source"], "fallback_template")
        self.assertEqual(payload["query_plan"]["subtype"], "absent_from_baseline_window")
        self.assertIn("plan_validator.tool.unknown", response.validation_issues)
        self.assertTrue(any(item.tool_name == "plan_validator" and not item.success for item in response.execution_trace))

    def test_unclassified_question_does_not_fall_back(self) -> None:
        question = "帮我做一个开放分析，看看最近的整体情况"
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)

        self.assertIsNone(decision.analytics_subtype)

        response = self.manager.run(
            request=request,
            routed_decision=merge_rule_and_llm(decision, None),
            strategy=_template_strategy(decision.confidence),
            mode="direct",
            llm_config=_llm_config(),
            execution_mode="direct",
        )
        payload = response.structured_output

        self.assertFalse(response.success)
        self.assertIsNone(payload["subtype"])
        self.assertEqual(
            payload["supported_subtypes"],
            [
                "absent_old_patients_recent_window",
                "absent_from_baseline_window",
                "doctors_with_active_plans",
            ],
        )

    def test_fixed_screen_risk_does_not_trigger_llm_router(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.SCREEN_RISK.value,
            therapist_id=56,
            days=30,
            raw_text="screen-risk --therapist-id 56 --days 30",
        )
        decision = self.router.route(request)

        self.assertEqual(decision.intent, "risk_screening")
        self.assertFalse(LLMRouter().should_refine(request, decision))

    def test_strategy_chooser_keeps_fixed_workflow_as_fixed(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.SCREEN_RISK.value,
            therapist_id=56,
            days=30,
            raw_text="screen-risk --therapist-id 56 --days 30",
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        strategy = _build_orchestrator().choose_execution_strategy(
            request,
            routed,
            mode="direct",
            llm_config=_llm_config(),
        )

        self.assertEqual(strategy.kind, "fixed_workflow")

    def test_strategy_chooser_keeps_standard_open_analytics_on_template(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=CASE_1_QUESTION,
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        strategy = _build_orchestrator().choose_execution_strategy(
            request,
            routed,
            mode="agents_sdk",
            llm_config=_agent_llm_config(),
        )

        self.assertEqual(strategy.kind, "template_analytics")

    def test_strategy_chooser_routes_complex_open_analytics_to_agent_runtime_when_available(self) -> None:
        orchestrator = _build_orchestrator()
        for question in (CASE_2_QUESTION, CASE_3_QUESTION):
            request = OrchestratorRequest(
                task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
                raw_text=question,
            )
            decision = self.router.route(request)
            routed = merge_rule_and_llm(decision, None)
            strategy = orchestrator.choose_execution_strategy(
                request,
                routed,
                mode="agents_sdk",
                llm_config=_agent_llm_config(),
            )

            self.assertEqual(strategy.kind, "agent_planned")

    def test_strategy_chooser_routes_complex_open_analytics_to_template_when_sdk_unavailable(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=CASE_2_QUESTION,
            use_agent_sdk=False,
        )
        decision = self.router.route(request)
        routed = merge_rule_and_llm(decision, None)
        strategy = _build_orchestrator().choose_execution_strategy(
            request,
            routed,
            mode="direct",
            llm_config=_llm_config(),
        )

        self.assertEqual(strategy.kind, "template_analytics")

    def test_agent_tool_specs_respect_analysis_scope(self) -> None:
        patient_request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=CASE_2_QUESTION,
        )
        patient_routed = merge_rule_and_llm(self.router.route(patient_request), None)
        patient_tool_names = {tool.tool_name for tool in self.manager._agent_tool_specs(patient_routed)}

        self.assertIn("list_patients_seen_by_doctor", patient_tool_names)
        self.assertIn("set_diff", patient_tool_names)
        self.assertNotIn("generate_review_card", patient_tool_names)
        self.assertNotIn("screen_risk_patients", patient_tool_names)

        aggregate_request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=CASE_3_QUESTION,
        )
        aggregate_routed = merge_rule_and_llm(self.router.route(aggregate_request), None)
        aggregate_tool_names = {tool.tool_name for tool in self.manager._agent_tool_specs(aggregate_routed)}

        self.assertEqual(aggregate_tool_names, {"list_doctors_with_active_plans"})

    def test_default_mode_uses_agents_sdk_when_llm_config_is_available(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=CASE_2_QUESTION,
        )

        mode, execution_mode, issues = _build_orchestrator()._resolve_mode(request, _agent_llm_config())

        self.assertEqual(mode, "agents_sdk")
        self.assertEqual(execution_mode, "agents_sdk")
        self.assertEqual(issues, [])

    def test_mode_falls_back_to_direct_when_default_llm_config_is_unavailable(self) -> None:
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=CASE_2_QUESTION,
        )

        mode, execution_mode, issues = _build_orchestrator()._resolve_mode(request, _llm_config())

        self.assertEqual(mode, "direct")
        self.assertEqual(execution_mode, "direct_fallback")
        self.assertEqual(issues, ["execution_mode.fallback_to_direct"])


if __name__ == "__main__":
    unittest.main()
