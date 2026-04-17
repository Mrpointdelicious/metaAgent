from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.orchestrator import RehabAgentOrchestrator
from agent.schemas import OrchestrationTaskType, OrchestratorRequest
from config import Settings
from repositories.db_client import DatabaseConnectionError
from server.request_factory import build_orchestrator_request_from_payload
from server.session_context import (
    MissingIdentityContextError,
    build_session_identity_context,
)


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for session identity tests")


def _build_orchestrator() -> RehabAgentOrchestrator:
    orchestrator = RehabAgentOrchestrator(Settings(use_mock_when_db_unavailable=True))
    orchestrator.repository.client.query = _raise_db_error  # type: ignore[method-assign]
    return orchestrator


class SessionIdentityTests(unittest.TestCase):
    def test_build_doctor_identity_from_doctor_id_only(self) -> None:
        identity = build_session_identity_context(doctor_id=59)

        self.assertEqual(identity.actor_role, "doctor")
        self.assertEqual(identity.actor_doctor_id, 59)
        self.assertEqual(identity.target_doctor_id, 59)
        self.assertIsNone(identity.target_patient_id)

    def test_build_patient_identity_from_patient_id_only(self) -> None:
        identity = build_session_identity_context(patient_id=146)

        self.assertEqual(identity.actor_role, "patient")
        self.assertEqual(identity.actor_patient_id, 146)
        self.assertEqual(identity.target_patient_id, 146)

    def test_build_doctor_identity_when_both_ids_are_present(self) -> None:
        identity = build_session_identity_context(doctor_id=59, patient_id=146)

        self.assertEqual(identity.actor_role, "doctor")
        self.assertEqual(identity.actor_doctor_id, 59)
        self.assertEqual(identity.target_patient_id, 146)

    def test_missing_identity_payload_is_rejected_by_factory(self) -> None:
        with self.assertRaises(MissingIdentityContextError):
            build_session_identity_context()

    def test_orchestrator_rejects_request_without_identity_context(self) -> None:
        response = _build_orchestrator().run(
            OrchestratorRequest(
                task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
                raw_text="show recent analytics",
            )
        )

        self.assertFalse(response.success)
        self.assertIn("missing_identity_context", response.validation_issues)
        self.assertEqual(response.execution_trace[0].tool_name, "identity_context")

    def test_doctor_identity_scopes_group_workflow(self) -> None:
        request = build_orchestrator_request_from_payload(
            {
                "doctor_id": 30001,
                "task_type": OrchestrationTaskType.SCREEN_RISK.value,
                "raw_text": "screen-risk",
                "days": 30,
                "top_k": 3,
            }
        )

        response = _build_orchestrator().run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["therapist_id"], 30001)
        self.assertTrue(any(item.tool_name == "identity_context" and item.success for item in response.execution_trace))

    def test_patient_identity_cannot_run_group_workflow(self) -> None:
        request = build_orchestrator_request_from_payload(
            {
                "patient_id": 20001,
                "task_type": OrchestrationTaskType.SCREEN_RISK.value,
                "raw_text": "screen-risk",
                "days": 30,
            }
        )

        response = _build_orchestrator().run(request)

        self.assertFalse(response.success)
        self.assertIn("authorization.patient_cannot_run_group_workflow", response.validation_issues)
        self.assertTrue(any(item.tool_name == "authorization_guard" for item in response.execution_trace))

    def test_doctor_cannot_access_patient_outside_scope(self) -> None:
        request = build_orchestrator_request_from_payload(
            {
                "doctor_id": 56,
                "patient_id": 20001,
                "task_type": OrchestrationTaskType.REVIEW_PATIENT.value,
                "raw_text": "review patient 20001",
                "days": 30,
            }
        )

        response = _build_orchestrator().run(request)

        self.assertFalse(response.success)
        self.assertIn("authorization.doctor_cannot_access_patient", response.validation_issues)


if __name__ == "__main__":
    unittest.main()
