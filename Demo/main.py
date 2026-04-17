"""Legacy local debug shell.

This file is kept for manual experiments only. It is not the production entry,
not the formal router, and not an identity source. It now requires an explicit
doctor or patient identity and builds requests through server.request_factory.
"""

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
    parser = argparse.ArgumentParser(description="Legacy local debug shell. Prefer server/main.py or explicit role demos.")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--doctor-id", type=int, help="Explicit doctor identity for this debug session.")
    identity.add_argument("--patient-id", type=int, help="Explicit patient identity for this debug session.")
    parser.add_argument("--question", help="Optional one-shot question. If omitted, starts an interactive debug loop.")
    parser.add_argument("--show-trace", action="store_true")
    parser.add_argument("--use-agent-sdk", action="store_true", default=None)
    parser.add_argument("--llm-provider", choices=["openai", "qwen", "deepseek"])
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-base-url")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    orchestrator = RehabAgentOrchestrator(settings)
    identity = build_session_identity_context(doctor_id=args.doctor_id, patient_id=args.patient_id)

    def run_text(text: str) -> None:
        request = build_orchestrator_request(
            raw_text=text,
            doctor_id=args.doctor_id,
            patient_id=args.patient_id,
            identity_context=identity,
            use_agent_sdk=args.use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
        )
        response = orchestrator.run(request)
        print_response(response, json_output=False, show_trace=args.show_trace)

    print(
        "Legacy local debug shell. Formal entry: server/main.py. "
        "Explicit demos: Demo/doctor_demo.py and Demo/patient_demo.py."
    )
    if args.question:
        run_text(args.question)
        return 0

    prompt = "legacy-doctor> " if args.doctor_id is not None else "legacy-patient> "
    while True:
        try:
            raw = input(prompt).strip()
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
