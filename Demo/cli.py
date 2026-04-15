from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import OrchestratorRequest, RehabAgentOrchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rehab execution deviation demo CLI")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print structured output as JSON")
    parser.add_argument("--use-agent-sdk", action="store_true", help="Use OpenAI Agents SDK when OPENAI_API_KEY is available")
    parser.add_argument("--llm-provider", choices=["openai", "qwen", "deepseek"], help="Switch provider for the current run")
    parser.add_argument("--llm-model", help="Override model name for the current run")
    parser.add_argument("--llm-base-url", help="Override base URL for the current run")

    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review-patient", help="Generate single-patient review card")
    review.add_argument("--patient-id", type=int)
    review.add_argument("--plan-id", type=int)
    review.add_argument("--therapist-id", type=int)
    review.add_argument("--days", type=int, default=30)

    screen = subparsers.add_parser("screen-risk", help="Screen therapist patients by risk")
    screen.add_argument("--therapist-id", type=int, required=True)
    screen.add_argument("--days", type=int, default=7)
    screen.add_argument("--top-k", type=int, default=10)

    weekly = subparsers.add_parser("weekly-report", help="Generate therapist weekly risk report")
    weekly.add_argument("--therapist-id", type=int, required=True)
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--top-k", type=int, default=10)

    return parser


def execute_args(args: argparse.Namespace) -> int:
    orchestrator = RehabAgentOrchestrator()
    should_use_agent_sdk = bool(
        args.use_agent_sdk or args.llm_provider or args.llm_model or args.llm_base_url
    )
    if args.command == "review-patient":
        request = OrchestratorRequest(
            task_type="single_review",
            patient_id=args.patient_id,
            plan_id=args.plan_id,
            therapist_id=args.therapist_id,
            days=args.days,
            raw_text="cli review-patient",
            use_agent_sdk=should_use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
        )
    elif args.command == "screen-risk":
        request = OrchestratorRequest(
            task_type="risk_screen",
            therapist_id=args.therapist_id,
            days=args.days,
            top_k=args.top_k,
            raw_text="cli screen-risk",
            use_agent_sdk=should_use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
        )
    else:
        request = OrchestratorRequest(
            task_type="weekly_report",
            therapist_id=args.therapist_id,
            days=args.days,
            top_k=args.top_k,
            raw_text="cli weekly-report",
            use_agent_sdk=should_use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
        )

    response = orchestrator.run(request)
    if args.json_output:
        print(
            json.dumps(
                {
                    "task_type": response.task_type,
                    "execution_mode": response.execution_mode,
                    "llm_provider": response.llm_provider,
                    "llm_model": response.llm_model,
                    "structured_output": response.structured_output,
                    "final_text": response.final_text,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"[execution_mode={response.execution_mode}, "
            f"llm_provider={response.llm_provider}, "
            f"llm_model={response.llm_model or 'default'}]"
        )
        print(response.final_text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return execute_args(args)


if __name__ == "__main__":
    raise SystemExit(main())
