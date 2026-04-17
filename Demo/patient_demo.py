from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import RehabAgentOrchestrator
from config import get_settings
from Demo.cli import print_response
from server.request_factory import build_orchestrator_request
from server.session_context import build_session_identity_context


def _configure_console_encoding() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patient identity demo entry.")
    parser.add_argument("--patient-id", type=int, required=True, help="Authenticated patient ID for this demo session.")
    parser.add_argument("--question", help="Optional one-shot question. If omitted, starts an interactive loop.")
    parser.add_argument("--show-trace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    orchestrator = RehabAgentOrchestrator(settings)
    identity = build_session_identity_context(patient_id=args.patient_id)

    def run_text(text: str) -> None:
        request = build_orchestrator_request(
            raw_text=text,
            patient_id=args.patient_id,
            identity_context=identity,
        )
        response = orchestrator.run(request)
        print_response(response, json_output=False, show_trace=args.show_trace)

    print(f"Patient demo started with patient_id={args.patient_id}.")
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
