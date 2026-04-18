from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import RehabAgentOrchestrator
from config import get_settings
from Demo.cli import print_response
from server.request_factory import build_orchestrator_request_from_payload, ensure_session_ids


def _configure_console_encoding() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patient identity demo entry.")
    parser.add_argument("--patient-id", type=int, required=True, help="Authenticated patient ID for this demo session.")
    parser.add_argument("--session-id", help="Frontend session_id to reuse for this demo run.")
    parser.add_argument("--conversation-id", help="Frontend conversation_id to reuse for this demo run.")
    parser.add_argument("--question", help="Optional one-shot question. If omitted, starts an interactive loop.")
    parser.add_argument("--show-trace", action="store_true")
    return parser


def build_demo_base_payload(
    *,
    patient_id: int,
    session_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "patient_id": patient_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
    }
    ensure_session_ids(payload)
    return payload


def build_turn_payload(base_payload: dict[str, Any], text: str) -> dict[str, Any]:
    payload = dict(base_payload)
    payload["question"] = text
    return payload


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    orchestrator = RehabAgentOrchestrator(settings)
    base_payload = build_demo_base_payload(
        patient_id=args.patient_id,
        session_id=args.session_id,
        conversation_id=args.conversation_id,
    )

    def run_text(text: str) -> None:
        request = build_orchestrator_request_from_payload(build_turn_payload(base_payload, text))
        response = orchestrator.run(request)
        print_response(response, json_output=False, show_trace=args.show_trace)

    print(
        "Patient demo started with "
        f"patient_id={args.patient_id}, "
        f"session_id={base_payload['session_id']}, "
        f"conversation_id={base_payload['conversation_id']}."
    )
    if args.question:
        run_text(args.question)
        return 0

    while True:
        try:
            raw = input("patient-demo> ").strip()
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
