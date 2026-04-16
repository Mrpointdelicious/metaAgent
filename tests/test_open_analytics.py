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
from agent.schemas import OrchestrationTaskType, OrchestratorRequest

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


def _llm_config() -> ResolvedLLMConfig:
    return ResolvedLLMConfig(provider="qwen", model="test-model")


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
            request,
            decision,
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
            request,
            decision,
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
            request,
            decision,
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

    def test_unclassified_question_does_not_fall_back(self) -> None:
        question = "帮我做一个开放分析，看看最近的整体情况"
        request = OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            raw_text=question,
        )
        decision = self.router.route(request)

        self.assertIsNone(decision.analytics_subtype)

        response = self.manager.run(
            request,
            decision,
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


if __name__ == "__main__":
    unittest.main()
