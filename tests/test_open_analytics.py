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
from agent.schemas import ExecutionStrategy, LLMPlannedQuery, LLMPlannedStep, OrchestrationTaskType, OrchestratorRequest

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


def _build_manager_with_planner(fake_planner) -> AnalyticsManager:  # noqa: ANN001
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

    def test_strategy_chooser_routes_complex_open_analytics_to_planner_when_available(self) -> None:
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
