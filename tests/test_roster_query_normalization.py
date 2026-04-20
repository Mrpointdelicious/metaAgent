from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.orchestrator import RehabAgentOrchestrator
from agent.roster_query import (
    extract_roster_days,
    extract_roster_limit,
    has_patient_roster_query,
    has_patient_roster_seed_query,
    has_patient_visit_semantics,
)
from agent.schemas import OrchestrationTaskType
from config import Settings
from repositories.db_client import DatabaseConnectionError
from server.request_factory import build_orchestrator_request
from server.session_context import build_session_identity_context


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for roster normalization tests")


def _build_orchestrator() -> RehabAgentOrchestrator:
    orchestrator = RehabAgentOrchestrator(Settings(use_mock_when_db_unavailable=True))
    orchestrator.repository.client.query = _raise_db_error  # type: ignore[method-assign]
    return orchestrator


def _doctor_request(text: str):
    return build_orchestrator_request(
        raw_text=text,
        doctor_id=30001,
        identity_context=build_session_identity_context(doctor_id=30001),
    )


class RosterQueryNormalizationTests(unittest.TestCase):
    def test_patient_roster_query_helper_covers_subject_action_and_visit_semantics(self) -> None:
        cases = {
            "列出我所有的病人": (None, None),
            "查询我所有病人": (None, None),
            "查找30天以来就诊的患者": (30, None),
            "最近100天来访的病人有哪些": (100, None),
            "查询一下我的病人，只显示前10个": (None, 10),
            "查询一下我的病人，按照最近来访进行排序，只列出前10位": (None, 10),
        }

        for text, (days, limit) in cases.items():
            with self.subTest(text=text):
                self.assertTrue(has_patient_roster_query(text))
                self.assertTrue(has_patient_roster_seed_query(text))
                self.assertEqual(extract_roster_days(text), days)
                self.assertEqual(extract_roster_limit(text), limit)

        self.assertTrue(has_patient_visit_semantics("最近100天来访的病人有哪些"))
        self.assertTrue(has_patient_visit_semantics("查找30天以来就诊的患者"))
        self.assertTrue(has_patient_visit_semantics("这些患者中哪些在这100天来康复过？"))
        self.assertFalse(has_patient_roster_seed_query("这些患者中哪些在这100天来康复过？"))

    def test_basic_patient_roster_queries_hit_lookup_chain(self) -> None:
        orchestrator = _build_orchestrator()
        for text in ("列出我所有的病人", "查询我所有病人"):
            with self.subTest(text=text):
                response = orchestrator.run(_doctor_request(text))

                self.assertTrue(response.success)
                self.assertEqual(response.task_type, OrchestrationTaskType.LOOKUP_QUERY.value)
                self.assertEqual(response.structured_output["lookup_subtype"], "list_my_patients")
                self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
                self.assertNotIn("fixed_workflow.review_patient_missing_slots", response.validation_issues)

    def test_visit_semantic_roster_queries_pass_days_to_list_my_patients(self) -> None:
        for text, expected_days in (
            ("查找30天以来就诊的患者", 30),
            ("最近100天来访的病人有哪些", 100),
        ):
            with self.subTest(text=text):
                response = _build_orchestrator().run(_doctor_request(text))

                self.assertTrue(response.success)
                self.assertEqual(response.task_type, OrchestrationTaskType.LOOKUP_QUERY.value)
                self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
                self.assertEqual(response.structured_output["days"], expected_days)
                self.assertNotIn("fixed_workflow.review_patient_missing_slots", response.validation_issues)

    def test_roster_limit_is_extracted_and_used_for_display(self) -> None:
        response = _build_orchestrator().run(_doctor_request("查询一下我的病人，只显示前10个"))

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, OrchestrationTaskType.LOOKUP_QUERY.value)
        self.assertEqual(response.structured_output["display_limit"], 10)
        self.assertLessEqual(response.structured_output["displayed_count"], 10)
        displayed_rows = [line for line in response.final_text.splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(displayed_rows), 10)

    def test_sort_wording_still_hits_roster_chain_without_fixed_workflow(self) -> None:
        response = _build_orchestrator().run(_doctor_request("查询一下我的病人，按照最近来访进行排序，只列出前10位"))

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, OrchestrationTaskType.LOOKUP_QUERY.value)
        self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
        self.assertEqual(response.structured_output["display_limit"], 10)
        self.assertNotIn("fixed_workflow.review_patient_missing_slots", response.validation_issues)

    def test_followup_without_active_result_set_returns_controlled_error(self) -> None:
        response = _build_orchestrator().run(_doctor_request("这些患者中哪些在这100天来康复过？"))

        self.assertFalse(response.success)
        self.assertEqual(response.task_type, OrchestrationTaskType.RESULT_SET_QUERY.value)
        self.assertEqual(response.structured_output["error"], "followup.missing_active_result_set")
        self.assertIn("followup.missing_active_result_set", response.validation_issues)
        self.assertNotIn("fixed_workflow.review_patient_missing_slots", response.validation_issues)

    def test_seed_decision_reuses_roster_query_helper(self) -> None:
        orchestrator = _build_orchestrator()
        for text in (
            "列出我所有的病人",
            "查询我所有病人",
            "查找30天以来就诊的患者",
            "最近100天来访的病人有哪些",
            "这些患者中哪些在这100天来康复过？",
        ):
            with self.subTest(text=text):
                request = _doctor_request(text)
                self.assertEqual(orchestrator._should_seed_patient_result_set(request), has_patient_roster_seed_query(text))

        source = inspect.getsource(RehabAgentOrchestrator._should_seed_patient_result_set)
        self.assertIn("has_patient_roster_seed_query", source)
        self.assertNotIn("我的患者", source)
        self.assertNotIn("my patients", source)


if __name__ == "__main__":
    unittest.main()
