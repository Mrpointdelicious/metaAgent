from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.orchestrator import RehabAgentOrchestrator
from agent.schemas import IntentDecision, RoutedDecision
from config import Settings
from repositories.db_client import DatabaseConnectionError
from server.request_factory import build_orchestrator_request
from server.session_context import build_session_identity_context


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for identity scoped user tool tests")


def _build_orchestrator() -> RehabAgentOrchestrator:
    orchestrator = RehabAgentOrchestrator(Settings(use_mock_when_db_unavailable=True))
    orchestrator.repository.client.query = _raise_db_error  # type: ignore[method-assign]
    return orchestrator


def _single_doctor_routed() -> RoutedDecision:
    rule = IntentDecision(
        intent="open_analytics_query",
        confidence=0.9,
        rationale="test",
        analysis_scope="single_doctor",
        doctor_id_source="session",
    )
    return RoutedDecision(
        rule_decision=rule,
        final_intent="open_analytics_query",
        final_scope="single_doctor",
        confidence=0.9,
        rationale="test",
    )


class IdentityScopedUserToolTests(unittest.TestCase):
    def test_repository_lists_related_patients_for_doctor(self) -> None:
        orchestrator = _build_orchestrator()
        rows = orchestrator.repository.get_related_patients_for_doctor(30001)

        self.assertEqual([row["patient_id"] for row in rows], [20001, 20002])
        self.assertEqual(rows[0]["patient_name"], "Mock Patient Alpha")

    def test_repository_lists_related_doctors_for_patient(self) -> None:
        orchestrator = _build_orchestrator()
        rows = orchestrator.repository.get_related_doctors_for_patient(20001)

        self.assertEqual([row["doctor_id"] for row in rows], [30001])
        self.assertEqual(rows[0]["doctor_name"], "Mock Doctor One")

    def test_doctor_can_lookup_own_name(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="我的名字",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        response = orchestrator.run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["source_tool"], "lookup_accessible_user_name")
        self.assertEqual(response.structured_output["user_name"], "Mock Doctor One")
        self.assertIn("Mock Doctor One", response.final_text)

    def test_doctor_can_lookup_related_patient_name(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="患者20001叫什么",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        response = orchestrator.run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["user_role"], "patient")
        self.assertEqual(response.structured_output["user_name"], "Mock Patient Alpha")

    def test_doctor_cannot_lookup_unrelated_patient_name(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="患者99999叫什么",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        response = orchestrator.run(request)

        self.assertFalse(response.success)
        self.assertEqual(response.structured_output["source_tool"], "lookup_accessible_user_name")
        self.assertFalse(response.structured_output["is_accessible"])
        self.assertIn("未找到可访问的用户信息", response.final_text)

    def test_doctor_lists_my_patients_without_review_routing(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="列出我所有的患者",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        response = orchestrator.run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "lookup_query")
        self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
        self.assertEqual(response.structured_output["count"], 2)
        self.assertIn("Mock Patient Alpha", response.final_text)

    def test_patient_lists_my_doctors(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="列出和我有关的医生",
            patient_id=20001,
            identity_context=build_session_identity_context(patient_id=20001),
        )

        response = orchestrator.run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["source_tool"], "list_my_doctors")
        self.assertEqual(response.structured_output["count"], 1)
        self.assertIn("Mock Doctor One", response.final_text)

    def test_patient_agent_whitelist_excludes_list_my_patients(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="复杂开放分析",
            patient_id=20001,
            identity_context=build_session_identity_context(patient_id=20001),
        )
        names = {tool.tool_name for tool in orchestrator.analytics_manager._agent_tool_specs(_single_doctor_routed(), request=request)}

        self.assertIn("lookup_accessible_user_name", names)
        self.assertIn("list_my_doctors", names)
        self.assertNotIn("list_my_patients", names)

    def test_doctor_agent_whitelist_excludes_list_my_doctors(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="复杂开放分析",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )
        names = {tool.tool_name for tool in orchestrator.analytics_manager._agent_tool_specs(_single_doctor_routed(), request=request)}

        self.assertIn("lookup_accessible_user_name", names)
        self.assertIn("list_my_patients", names)
        self.assertNotIn("list_my_doctors", names)

    def test_thirty_day_patient_roster_uses_list_my_patients(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="列出当前30天所有的患者",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        response = orchestrator.run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
        self.assertEqual(response.structured_output["days"], 30)


if __name__ == "__main__":
    unittest.main()
