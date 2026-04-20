from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.orchestrator import RehabAgentOrchestrator
from agent.schemas import OrchestratorRequest
from config import Settings
from repositories.db_client import DatabaseConnectionError
from server.request_factory import build_orchestrator_request
from server.result_set_store import ResultSetStore
from server.session_context import build_session_identity_context


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for result-set follow-up tests")


def _settings() -> Settings:
    return Settings(use_mock_when_db_unavailable=True, result_set_ttl_seconds=86400)


def _build_orchestrator(store: ResultSetStore | None = None) -> RehabAgentOrchestrator:
    settings = _settings()
    orchestrator = RehabAgentOrchestrator(settings, result_set_store=store or ResultSetStore(settings))
    orchestrator.repository.client.query = _raise_db_error  # type: ignore[method-assign]
    return orchestrator


def _doctor_request(text: str, *, session_id: str = "rs-session", conversation_id: str = "rs-conv") -> OrchestratorRequest:
    return build_orchestrator_request(
        raw_text=text,
        doctor_id=30001,
        identity_context=build_session_identity_context(
            doctor_id=30001,
            session_id=session_id,
            conversation_id=conversation_id,
        ),
    )


class ResultSetFollowupTests(unittest.TestCase):
    def test_roster_lookup_registers_active_result_set(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        request = _doctor_request("list my patients")

        response = orchestrator.run(request)
        context = store.get_thread_context(request.identity_context)

        self.assertTrue(response.success)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
        self.assertTrue(response.structured_output["result_set_id"].startswith("rs_"))
        self.assertEqual(context.active_result_set_id, response.structured_output["result_set_id"])
        self.assertEqual(context.active_result_set_type, "patient_set")
        self.assertEqual(context.active_result_count, 2)
        self.assertIsNone(context.default_time_window_days)
        self.assertTrue(any(item.tool_name == "result_set_store" for item in response.execution_trace))

    def test_followup_filter_overwrites_active_result_set_and_default_days(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        seed_request = _doctor_request("list my patients", session_id="s-training", conversation_id="c1")
        orchestrator.run(seed_request)
        seed_context = store.get_thread_context(seed_request.identity_context)
        assert seed_context is not None
        seed_result_set_id = seed_context.active_result_set_id

        followup_request = _doctor_request(
            "these patients with training in the last 30 days",
            session_id="s-training",
            conversation_id="c1",
        )
        response = orchestrator.run(followup_request)
        context = store.get_thread_context(followup_request.identity_context)

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertEqual(response.structured_output["source_tool"], "filter_result_set_by_training")
        self.assertNotEqual(response.structured_output["result_set_id"], seed_result_set_id)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.active_result_set_id, response.structured_output["result_set_id"])
        self.assertEqual(context.active_result_set_type, "patient_set")
        self.assertEqual(context.default_time_window_days, 30)
        self.assertFalse(any(item.tool_name == "strategy_chooser" for item in response.execution_trace))

    def test_enrich_collection_overwrites_active_result_set(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        completion_request = _doctor_request("my patients completed plans in the last 30 days", session_id="s-enrich", conversation_id="c1")
        completion_response = orchestrator.run(completion_request)
        completion_context = store.get_thread_context(completion_request.identity_context)
        assert completion_context is not None
        completion_result_set_id = completion_context.active_result_set_id

        enrich_request = _doctor_request("show their completion time", session_id="s-enrich", conversation_id="c1")
        response = orchestrator.run(enrich_request)
        context = store.get_thread_context(enrich_request.identity_context)

        self.assertTrue(completion_response.success)
        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertEqual(response.structured_output["source_tool"], "enrich_result_set_with_completion_time")
        self.assertNotEqual(response.structured_output["result_set_id"], completion_result_set_id)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.active_result_set_id, response.structured_output["result_set_id"])
        self.assertEqual(context.default_time_window_days, 30)
        self.assertEqual(context.active_result_count, response.structured_output["count"])

    def test_lookup_does_not_overwrite_active_result_set(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        seed_request = _doctor_request("list my patients", session_id="s-lookup", conversation_id="c1")
        orchestrator.run(seed_request)
        before_context = store.get_thread_context(seed_request.identity_context)
        assert before_context is not None

        lookup_request = _doctor_request("my name", session_id="s-lookup", conversation_id="c1")
        response = orchestrator.run(lookup_request)
        after_context = store.get_thread_context(lookup_request.identity_context)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["source_tool"], "lookup_accessible_user_name")
        self.assertIsNotNone(after_context)
        assert after_context is not None
        self.assertEqual(after_context.active_result_set_id, before_context.active_result_set_id)

    def test_tool_failure_does_not_overwrite_active_result_set(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        seed_request = _doctor_request("list my patients", session_id="s-failure", conversation_id="c1")
        orchestrator.run(seed_request)
        before_context = store.get_thread_context(seed_request.identity_context)
        assert before_context is not None

        def fail_filter(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            del args, kwargs
            raise RuntimeError("forced filter failure")

        orchestrator.result_set_service.filter_result_set_by_training = fail_filter  # type: ignore[method-assign]
        response = orchestrator.run(
            _doctor_request("these patients with training in the last 30 days", session_id="s-failure", conversation_id="c1")
        )
        after_context = store.get_thread_context(seed_request.identity_context)

        self.assertFalse(response.success)
        self.assertIn("result_set.operation_failed", response.validation_issues)
        self.assertIsNotNone(after_context)
        assert after_context is not None
        self.assertEqual(after_context.active_result_set_id, before_context.active_result_set_id)

    def test_followup_without_active_result_set_returns_controlled_error(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)

        response = orchestrator.run(_doctor_request("these patients absent in the last 30 days", session_id="s-empty", conversation_id="c1"))

        self.assertFalse(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertIn("followup.missing_active_result_set", response.validation_issues)
        self.assertFalse(any(item.tool_name == "strategy_chooser" for item in response.execution_trace))

    def test_same_session_different_conversation_does_not_share_active_result_set(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        orchestrator.run(_doctor_request("list my patients", session_id="same-session", conversation_id="c1"))

        response = orchestrator.run(_doctor_request("these patients absent in the last 30 days", session_id="same-session", conversation_id="c2"))

        self.assertFalse(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertIn("followup.missing_active_result_set", response.validation_issues)

    def test_roster_display_prefers_names_while_structured_rows_keep_ids(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)

        response = orchestrator.run(_doctor_request("list my patients", session_id="s-display", conversation_id="c1"))

        self.assertIn("Mock Patient Alpha", response.final_text)
        self.assertEqual(response.structured_output["rows"][0]["patient_id"], 20001)


if __name__ == "__main__":
    unittest.main()
