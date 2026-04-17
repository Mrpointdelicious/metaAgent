from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import RehabAgentOrchestrator
from agent.schemas import OrchestratorRequest
from config import get_settings
from Demo.cli import print_response
from Demo.dialogue import ConversationState, parse_natural_language_request, update_state_from_response
from server.session_context import build_session_identity_context


def _configure_console_encoding() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def _attach_doctor_identity(request: OrchestratorRequest, doctor_id: int) -> OrchestratorRequest:
    identity = build_session_identity_context(doctor_id=doctor_id, patient_id=request.patient_id)
    return request.model_copy(
        update={
            "doctor_id": doctor_id,
            "therapist_id": doctor_id,
            "identity_context": identity,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Doctor identity demo entry.")
    parser.add_argument("--doctor-id", type=int, required=True, help="Authenticated doctor ID for this demo session.")
    parser.add_argument("--question", help="Optional one-shot question. If omitted, starts an interactive loop.")
    parser.add_argument("--show-trace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    orchestrator = RehabAgentOrchestrator(settings)
    state = ConversationState(therapist_id=args.doctor_id)

    def run_text(text: str) -> None:
        request, note = parse_natural_language_request(
            text,
            state,
            settings,
            use_agent_sdk=None,
            llm_provider=None,
            llm_model=None,
            llm_base_url=None,
        )
        if note:
            print(note)
        if request is None:
            return
        request = _attach_doctor_identity(request, args.doctor_id)
        response = orchestrator.run(request)
        print_response(response, json_output=False, show_trace=args.show_trace)
        update_state_from_response(state, request, response)

    print(f"Doctor demo started with doctor_id={args.doctor_id}.")
    if args.question:
        run_text(args.question)
        return 0

    while True:
        try:
            raw = input("doctor-demo> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        if raw.lower() in {"exit", "quit"}:
            return 0
        run_text(raw)


if __name__ == "__main__":
    raise SystemExit(main())
