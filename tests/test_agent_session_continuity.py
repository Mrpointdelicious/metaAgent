from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents import function_tool

from agent.open_analytics_agent import OpenAnalyticsAgentRuntime
from agent.schemas import IntentDecision, RoutedDecision
from config import ResolvedLLMConfig, Settings
from Demo import doctor_demo, patient_demo
from server.request_factory import build_orchestrator_request_from_payload
from server.session_manager import AgentSessionManager
from tools.base import DoctorWindowInput, ToolSpec


def _memory_settings() -> Settings:
    return Settings(agent_session_backend="memory", use_mock_when_db_unavailable=True)


def _llm_config() -> ResolvedLLMConfig:
    return ResolvedLLMConfig(provider="qwen", api_key="test-key", model="test-model", base_url="http://localhost")


def _routed_decision() -> RoutedDecision:
    rule = IntentDecision(
        intent="open_analytics_query",
        confidence=0.9,
        rationale="unit test session continuity",
        analytics_subtype="absent_from_baseline_window",
        analysis_scope="single_doctor",
        doctor_id_source="session",
    )
    return RoutedDecision(
        rule_decision=rule,
        final_intent="open_analytics_query",
        final_subtype="absent_from_baseline_window",
        final_scope="single_doctor",
        doctor_id_source="session",
        confidence=0.9,
        rationale="unit test session continuity",
    )


def _tool_spec() -> ToolSpec:
    @function_tool
    def list_patients_seen_by_doctor(
        doctor_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str = "attendance",
    ) -> dict:
        """Return a patient set for session continuity tests."""
        del doctor_id, start_date, end_date, source
        return {"set_id": "unit-test", "patient_ids": [], "count": 0}

    return ToolSpec(
        tool_name="list_patients_seen_by_doctor",
        description="Return patient set.",
        input_model=DoctorWindowInput,
        output_schema="PatientSet JSON",
        chain_scope="A",
        can_affect_risk_score=False,
        direct_handler=lambda **kwargs: {"set_id": "unit-test", "patient_ids": [], "count": 0},
        agent_tool=list_patients_seen_by_doctor,
    )


def _request(session_id: str, conversation_id: str, question: str):
    return build_orchestrator_request_from_payload(
        {
            "doctor_id": 56,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "question": question,
            "use_agent_sdk": True,
        }
    )


class AgentSessionContinuityTests(unittest.TestCase):
    def test_runtime_passes_same_sdk_session_for_follow_up_turn(self) -> None:
        manager = AgentSessionManager(_memory_settings())
        runtime = OpenAnalyticsAgentRuntime(settings=_memory_settings(), session_manager=manager)
        histories: list[list[dict]] = []
        sessions: list[object] = []

        def fake_run_sync(agent, run_input, **kwargs):  # noqa: ANN001, ANN202
            del agent
            session = kwargs["session"]
            sessions.append(session)
            histories.append(asyncio.run(session.get_items()))
            asyncio.run(session.add_items([{"role": "user", "content": run_input}]))
            return SimpleNamespace(
                final_output=json.dumps(
                    {
                        "normalized_question": "unit test",
                        "final_text": "ok",
                        "structured_output": {"summary": "ok", "fallback_required": False},
                    }
                ),
                new_items=[],
            )

        with patch("agents.Runner.run_sync", side_effect=fake_run_sync):
            runtime.run(
                request=_request("s1", "c1", "list my patients"),
                routed_decision=_routed_decision(),
                tool_specs=[_tool_spec()],
                llm_config=_llm_config(),
            )
            runtime.run(
                request=_request("s1", "c1", "which of these patients trained in the last 30 days?"),
                routed_decision=_routed_decision(),
                tool_specs=[_tool_spec()],
                llm_config=_llm_config(),
            )

        self.assertIs(sessions[0], sessions[1])
        self.assertEqual(histories[0], [])
        self.assertEqual(len(histories[1]), 1)
        self.assertIn("list my patients", histories[1][0]["content"])

    def test_different_session_ids_do_not_share_raw_history_when_used_directly(self) -> None:
        manager = AgentSessionManager(_memory_settings())
        s1 = manager.get_or_create_session("s1")
        s2 = manager.get_or_create_session("s2")

        asyncio.run(s1.add_items([{"role": "user", "content": "first session"}]))

        self.assertIsNot(s1, s2)
        self.assertEqual(asyncio.run(s2.get_items()), [])

    def test_same_conversation_different_session_ids_share_thread_history(self) -> None:
        manager = AgentSessionManager(_memory_settings())
        runtime = OpenAnalyticsAgentRuntime(settings=_memory_settings(), session_manager=manager)
        request_1 = _request("s1", "same-conversation", "first turn")
        request_2 = _request("s2", "same-conversation", "second turn")
        session_1 = runtime._session_for_request(request_1)
        session_2 = runtime._session_for_request(request_2)

        asyncio.run(session_1.add_items([{"role": "user", "content": "session one only"}]))

        self.assertEqual(request_1.identity_context.conversation_id, request_2.identity_context.conversation_id)
        self.assertIs(session_1, session_2)
        self.assertEqual(asyncio.run(session_2.get_items()), [{"role": "user", "content": "session one only"}])

    def test_same_session_different_conversation_ids_do_not_share_thread_history(self) -> None:
        manager = AgentSessionManager(_memory_settings())
        runtime = OpenAnalyticsAgentRuntime(settings=_memory_settings(), session_manager=manager)
        request_1 = _request("same-session", "c1", "first turn")
        request_2 = _request("same-session", "c2", "second turn")
        session_1 = runtime._session_for_request(request_1)
        session_2 = runtime._session_for_request(request_2)

        asyncio.run(session_1.add_items([{"role": "user", "content": "conversation one only"}]))

        self.assertEqual(request_1.identity_context.session_id, request_2.identity_context.session_id)
        self.assertIsNot(session_1, session_2)
        self.assertEqual(asyncio.run(session_2.get_items()), [])

    def test_doctor_demo_turn_payload_reuses_one_session_and_conversation(self) -> None:
        base_payload = doctor_demo.build_demo_base_payload(
            doctor_id=56,
            session_id="s1",
            conversation_id="c1",
        )
        turn_1 = doctor_demo.build_turn_payload(base_payload, "list my patients")
        turn_2 = doctor_demo.build_turn_payload(base_payload, "which of these patients trained?")

        self.assertEqual(turn_1["session_id"], "s1")
        self.assertEqual(turn_2["session_id"], "s1")
        self.assertEqual(turn_1["conversation_id"], "c1")
        self.assertEqual(turn_2["conversation_id"], "c1")
        self.assertEqual(build_orchestrator_request_from_payload(turn_1).identity_context.session_id, "s1")
        self.assertEqual(build_orchestrator_request_from_payload(turn_2).identity_context.conversation_id, "c1")

    def test_patient_demo_turn_payload_reuses_one_session_and_conversation(self) -> None:
        base_payload = patient_demo.build_demo_base_payload(
            patient_id=20001,
            session_id="patient-s1",
            conversation_id="patient-c1",
        )
        turn_1 = patient_demo.build_turn_payload(base_payload, "my plans")
        turn_2 = patient_demo.build_turn_payload(base_payload, "which previous items are unfinished?")

        self.assertEqual(turn_1["session_id"], "patient-s1")
        self.assertEqual(turn_2["session_id"], "patient-s1")
        self.assertEqual(turn_1["conversation_id"], "patient-c1")
        self.assertEqual(turn_2["conversation_id"], "patient-c1")


if __name__ == "__main__":
    unittest.main()
