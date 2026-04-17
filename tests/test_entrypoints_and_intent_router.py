from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.intent_router import IntentRouter
from agent.orchestrator import RehabAgentOrchestrator
from agent.schemas import OrchestrationTaskType, OrchestratorRequest
from config import Settings
from repositories.db_client import DatabaseConnectionError
from server.main import handle_payload
from server.request_factory import build_orchestrator_request, build_orchestrator_request_from_payload
from server.session_context import build_session_identity_context


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for entrypoint/router tests")


def _build_orchestrator() -> RehabAgentOrchestrator:
    orchestrator = RehabAgentOrchestrator(Settings(use_mock_when_db_unavailable=True))
    orchestrator.repository.client.query = _raise_db_error  # type: ignore[method-assign]
    return orchestrator


class EntrypointAndIntentRouterTests(unittest.TestCase):
    def test_server_payload_uses_request_factory_and_core_orchestrator(self) -> None:
        response = handle_payload({"doctor_id": 30001, "question": "看一下最近7天高风险患者", "days": 7})

        self.assertTrue(response["success"])
        self.assertEqual(response["task_type"], OrchestrationTaskType.SCREEN_RISK.value)
        self.assertTrue(any(item["tool_name"] == "identity_context" for item in response["execution_trace"]))

    def test_request_factory_injects_doctor_identity(self) -> None:
        request = build_orchestrator_request_from_payload({"doctor_id": 30001, "question": "查询医生30001的名字"})

        self.assertEqual(request.identity_context.actor_role, "doctor")
        self.assertEqual(request.identity_context.actor_doctor_id, 30001)
        self.assertEqual(request.raw_text, "查询医生30001的名字")

    def test_request_factory_accepts_query_alias(self) -> None:
        request = build_orchestrator_request_from_payload({"doctor_id": 30001, "query": "看一下最近7天高风险患者"})

        self.assertEqual(request.identity_context.actor_role, "doctor")
        self.assertEqual(request.raw_text, "看一下最近7天高风险患者")

    def test_doctor_and_patient_demos_do_not_import_dialogue_router(self) -> None:
        doctor_source = Path("Demo/doctor_demo.py").read_text(encoding="utf-8")
        patient_source = Path("Demo/patient_demo.py").read_text(encoding="utf-8")
        legacy_main_source = Path("Demo/main.py").read_text(encoding="utf-8")

        self.assertNotIn("parse_natural_language_request", doctor_source)
        self.assertNotIn("parse_natural_language_request", patient_source)
        self.assertNotIn("parse_natural_language_request", legacy_main_source)
        self.assertIn("build_orchestrator_request", doctor_source)
        self.assertIn("build_orchestrator_request", patient_source)
        self.assertIn("build_orchestrator_request", legacy_main_source)

    def test_doctor_name_lookup_does_not_route_to_risk_screening(self) -> None:
        identity = build_session_identity_context(doctor_id=30001)
        request = build_orchestrator_request(
            raw_text="查询医生30001的名字",
            doctor_id=30001,
            identity_context=identity,
        )
        decision = IntentRouter().route(request)

        self.assertEqual(decision.intent, "lookup_query")
        self.assertEqual(decision.lookup_entity_type, "doctor")
        self.assertEqual(decision.lookup_user_id, 30001)

        response = _build_orchestrator().run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, OrchestrationTaskType.LOOKUP_QUERY.value)
        self.assertEqual(response.structured_output["user_name"], "Mock Doctor One")
        self.assertIn("Mock Doctor One", response.final_text)

    def test_patient_name_lookup_does_not_route_to_review(self) -> None:
        identity = build_session_identity_context(doctor_id=30001)
        request = build_orchestrator_request(
            raw_text="患者20001叫什么",
            doctor_id=30001,
            identity_context=identity,
        )
        decision = IntentRouter().route(request)

        self.assertEqual(decision.intent, "lookup_query")
        self.assertEqual(decision.lookup_entity_type, "patient")
        self.assertEqual(decision.lookup_user_id, 20001)

        response = _build_orchestrator().run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["user_name"], "Mock Patient Alpha")
        self.assertIn("Mock Patient Alpha", response.final_text)

    def test_patient_identity_cannot_lookup_other_patient(self) -> None:
        request = build_orchestrator_request(
            raw_text="患者20002叫什么",
            patient_id=20001,
            identity_context=build_session_identity_context(patient_id=20001),
        )

        response = _build_orchestrator().run(request)

        self.assertFalse(response.success)
        self.assertIn("authorization.patient_cannot_access_lookup_target", response.validation_issues)

    def test_bare_entity_lookup_uses_lookup_path(self) -> None:
        request = build_orchestrator_request(
            raw_text="30001是谁",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )
        decision = IntentRouter().route(request)

        self.assertEqual(decision.intent, "lookup_query")
        self.assertEqual(decision.lookup_entity_type, "unknown")
        self.assertEqual(decision.lookup_user_id, 30001)

    def test_identifier_only_request_is_conservative_not_fixed_workflow(self) -> None:
        request = OrchestratorRequest(
            raw_text="患者20001",
            identity_context=build_session_identity_context(doctor_id=30001),
            doctor_id=30001,
        )
        decision = IntentRouter().route(request)

        self.assertEqual(decision.intent, "open_analytics_query")
        self.assertLess(decision.confidence, 0.75)
        self.assertIsNone(decision.analytics_subtype)


if __name__ == "__main__":
    unittest.main()
