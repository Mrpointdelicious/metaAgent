from __future__ import annotations

import ast
import sys
import unittest
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.orchestrator import FixedWorkflowEntryError, RehabAgentOrchestrator
from agent.schemas import IntentDecision, OrchestrationTaskType, RoutedDecision, StepExecutionResult
from config import ResolvedLLMConfig, Settings
from repositories.db_client import DatabaseConnectionError
from server.request_factory import build_orchestrator_request
from server.session_context import build_session_identity_context


def _raise_db_error(sql, params=None):  # noqa: ANN001, ANN201
    del sql, params
    raise DatabaseConnectionError("forced mock backend for orchestrator cleanup tests")


def _build_orchestrator() -> RehabAgentOrchestrator:
    orchestrator = RehabAgentOrchestrator(Settings(use_mock_when_db_unavailable=True))
    orchestrator.repository.client.query = _raise_db_error  # type: ignore[method-assign]
    return orchestrator


class OrchestratorCleanupTests(unittest.TestCase):
    def test_key_orchestrator_methods_have_single_definition(self) -> None:
        source = Path("agent/orchestrator.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        orchestrator_class = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "RehabAgentOrchestrator")
        method_names = [node.name for node in orchestrator_class.body if isinstance(node, ast.FunctionDef)]
        counts = Counter(method_names)

        for name in (
            "_run_lookup_query",
            "_lookup_entity_label",
            "_extract_identifier",
            "_extract_days",
            "_extract_slots",
        ):
            self.assertEqual(counts[name], 1, name)

    def test_review_patient_missing_slots_raises_in_fixed_workflow(self) -> None:
        request = build_orchestrator_request(
            task_type=OrchestrationTaskType.REVIEW_PATIENT.value,
            raw_text="review this patient",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        with self.assertRaisesRegex(FixedWorkflowEntryError, "fixed_workflow.review_patient_missing_slots"):
            _build_orchestrator().run(request)

    def test_patient_roster_queries_do_not_fall_into_fixed_review(self) -> None:
        orchestrator = _build_orchestrator()
        for text in ("列出我所有的病人", "查询我所有病人"):
            request = build_orchestrator_request(
                raw_text=text,
                doctor_id=30001,
                identity_context=build_session_identity_context(doctor_id=30001),
            )

            response = orchestrator.run(request)

            self.assertTrue(response.success)
            self.assertEqual(response.task_type, OrchestrationTaskType.LOOKUP_QUERY.value)
            self.assertEqual(response.structured_output["source_tool"], "list_my_patients")
            self.assertNotEqual(response.task_type, OrchestrationTaskType.REVIEW_PATIENT.value)

    def test_roster_display_uses_single_limit_source(self) -> None:
        request = build_orchestrator_request(
            raw_text="列出我所有的病人，只显示前1个",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )

        response = _build_orchestrator().run(request)

        self.assertTrue(response.success)
        self.assertEqual(response.structured_output["display_limit"], 1)
        self.assertEqual(response.structured_output["displayed_count"], 1)
        displayed_rows = [line for line in response.final_text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(displayed_rows), 1)

    def test_fixed_workflow_unsupported_entry_raises(self) -> None:
        orchestrator = _build_orchestrator()
        request = build_orchestrator_request(
            raw_text="general unsupported request",
            doctor_id=30001,
            identity_context=build_session_identity_context(doctor_id=30001),
        )
        routed = RoutedDecision(
            rule_decision=IntentDecision(intent="open_analytics_query", confidence=0.8, rationale="unit test"),
            final_intent="open_analytics_query",
            confidence=0.8,
            rationale="unit test unsupported fixed entry",
        )
        trace = StepExecutionResult(step_id="unit", tool_name="unit", success=True, args={}, output_summary="unit")

        with self.assertRaisesRegex(FixedWorkflowEntryError, "fixed_workflow.unsupported_entry"):
            orchestrator._run_fixed_workflow(
                request=request,
                routed=routed,
                route_trace=trace,
                strategy_trace=trace,
                identity_trace=trace,
                mode="direct",
                llm_config=ResolvedLLMConfig(provider="qwen", model="test-model"),
                execution_mode="direct",
                mode_issues=[],
            )


if __name__ == "__main__":
    unittest.main()
