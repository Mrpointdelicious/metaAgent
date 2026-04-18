from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.orchestrator import RehabAgentOrchestrator
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


def _doctor_request(text: str, *, session_id: str = "rs-session", conversation_id: str = "rs-conv"):
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

        response = orchestrator.run(_doctor_request("列出我所有的患者"))

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
        self.assertIn("active_result_set", response.structured_output)
        self.assertEqual(response.structured_output["active_result_set"]["result_set_type"], "patient_set")
        self.assertEqual(response.structured_output["active_result_set"]["count"], 2)
        self.assertTrue(any(item.tool_name == "result_set_store" for item in response.execution_trace))

    def test_followup_filters_active_result_set_by_training(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        orchestrator.run(_doctor_request("列出我所有的患者", session_id="s-training", conversation_id="c1"))

        response = orchestrator.run(_doctor_request("这些患者中哪些在这30天内有训练？", session_id="s-training", conversation_id="c1"))

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertEqual(response.structured_output["source_tool"], "filter_result_set_by_training")
        self.assertTrue(any(item.tool_name == "filter_result_set_by_training" for item in response.execution_trace))
        self.assertFalse(any(item.tool_name == "strategy_chooser" for item in response.execution_trace))

    def test_followup_filters_active_result_set_by_absence(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        orchestrator.run(_doctor_request("列出我所有的患者", session_id="s-absence", conversation_id="c1"))

        response = orchestrator.run(_doctor_request("以上这些患者里哪些没来？", session_id="s-absence", conversation_id="c1"))

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertEqual(response.structured_output["source_tool"], "filter_result_set_by_absence")
        self.assertTrue(any(item.tool_name == "filter_result_set_by_absence" for item in response.execution_trace))

    def test_my_patients_completion_filter_seeds_roster_without_fixed_workflow(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)

        response = orchestrator.run(_doctor_request("我的患者有哪些在这30天内完成了训练计划？", session_id="s-completion", conversation_id="c1"))

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertEqual(response.structured_output["source_tool"], "filter_result_set_by_plan_completion")
        self.assertTrue(any(item.tool_name == "list_my_patients" for item in response.execution_trace))
        self.assertTrue(any(item.tool_name == "filter_result_set_by_plan_completion" for item in response.execution_trace))
        self.assertFalse(any(item.tool_name == "strategy_chooser" for item in response.execution_trace))

    def test_enriches_active_result_set_with_completion_time(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)
        orchestrator.run(_doctor_request("我的患者有哪些在这30天内完成了训练计划？", session_id="s-enrich", conversation_id="c1"))

        response = orchestrator.run(_doctor_request("显示他们完成计划的具体时间", session_id="s-enrich", conversation_id="c1"))

        self.assertTrue(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertEqual(response.structured_output["source_tool"], "enrich_result_set_with_completion_time")
        self.assertTrue(any(row.get("completion_time") for row in response.structured_output["rows"]))

    def test_followup_without_active_result_set_returns_controlled_error(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)

        response = orchestrator.run(_doctor_request("这些患者中哪些没来？", session_id="s-empty", conversation_id="c1"))

        self.assertFalse(response.success)
        self.assertEqual(response.task_type, "result_set_query")
        self.assertIn("followup.missing_active_result_set", response.validation_issues)
        self.assertFalse(any(item.tool_name == "strategy_chooser" for item in response.execution_trace))

    def test_roster_display_prefers_names_while_structured_rows_keep_ids(self) -> None:
        store = ResultSetStore(_settings())
        orchestrator = _build_orchestrator(store)

        response = orchestrator.run(_doctor_request("列出我所有的患者", session_id="s-display", conversation_id="c1"))

        self.assertIn("Mock Patient Alpha", response.final_text)
        self.assertNotIn("患者20001）", response.final_text)
        self.assertEqual(response.structured_output["rows"][0]["patient_id"], 20001)


if __name__ == "__main__":
    unittest.main()
