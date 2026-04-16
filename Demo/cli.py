from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import OrchestratorRequest, RehabAgentOrchestrator
from agent.schemas import OrchestrationTaskType
from config import get_settings


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", dest="json_output", help="以 JSON 输出结构化结果")
    common.add_argument("--show-trace", action="store_true", help="在非 JSON 模式下打印执行轨迹")
    common.add_argument("--use-agent-sdk", action="store_true", default=None, help="当 provider 凭据可用时启用 agents_sdk 模式")
    common.add_argument("--llm-provider", choices=["openai", "qwen", "deepseek"], help="为本次运行切换 provider")
    common.add_argument("--llm-model", help="覆盖本次运行使用的模型名")
    common.add_argument("--llm-base-url", help="覆盖本次运行使用的 base URL")

    parser = argparse.ArgumentParser(description="康复计划执行偏离复核 Demo CLI", parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review-patient", help="生成单患者复核卡", parents=[common])
    review.add_argument("--patient-id", type=int)
    review.add_argument("--plan-id", type=int)
    review.add_argument("--therapist-id", type=int)
    review.add_argument("--days", type=int, default=30)

    screen = subparsers.add_parser("screen-risk", help="按风险筛选治疗师名下患者", parents=[common])
    screen.add_argument("--therapist-id", type=int)
    screen.add_argument("--days", type=int, default=7)
    screen.add_argument("--top-k", type=int, default=10)

    weekly = subparsers.add_parser("weekly-report", help="生成治疗师周报", parents=[common])
    weekly.add_argument("--therapist-id", type=int)
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--top-k", type=int, default=10)

    ask = subparsers.add_parser("ask", help="执行开放式分析问句", parents=[common])
    ask.add_argument("question")
    ask.add_argument("--therapist-id", type=int)
    ask.add_argument("--days", type=int, default=30)
    ask.add_argument("--top-k", type=int, default=20)

    return parser


def apply_global_overrides_from_argv(args: argparse.Namespace, argv: list[str]) -> argparse.Namespace:
    args.json_output = getattr(args, "json_output", False) or ("--json" in argv)
    args.show_trace = getattr(args, "show_trace", False) or ("--show-trace" in argv)
    args.use_agent_sdk = True if "--use-agent-sdk" in argv else getattr(args, "use_agent_sdk", None)
    for option_name in ("llm_provider", "llm_model", "llm_base_url"):
        current_value = getattr(args, option_name, None)
        if current_value:
            continue
        option_flag = f"--{option_name.replace('_', '-')}"
        if option_flag in argv:
            index = argv.index(option_flag)
            if index + 1 < len(argv):
                setattr(args, option_name, argv[index + 1])
    return args


def build_request_from_args(args: argparse.Namespace) -> OrchestratorRequest:
    settings = get_settings()
    default_therapist_id = settings.demo_default_therapist_id
    default_plan_id = settings.demo_default_plan_id
    use_agent_sdk = args.use_agent_sdk
    if args.command == "review-patient":
        return OrchestratorRequest(
            task_type=OrchestrationTaskType.REVIEW_PATIENT.value,
            patient_id=args.patient_id,
            plan_id=args.plan_id or default_plan_id,
            therapist_id=args.therapist_id,
            days=args.days,
            raw_text="cli review-patient",
            use_agent_sdk=use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
            context={},
        )
    if args.command == "screen-risk":
        return OrchestratorRequest(
            task_type=OrchestrationTaskType.SCREEN_RISK.value,
            therapist_id=args.therapist_id or default_therapist_id,
            days=args.days,
            top_k=args.top_k,
            raw_text="cli screen-risk",
            use_agent_sdk=use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
            context={},
        )
    if args.command == "ask":
        return OrchestratorRequest(
            task_type=OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value,
            therapist_id=args.therapist_id,
            days=args.days,
            top_k=args.top_k,
            raw_text=args.question,
            use_agent_sdk=use_agent_sdk,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
            context={},
        )
    return OrchestratorRequest(
        task_type=OrchestrationTaskType.WEEKLY_REPORT.value,
        therapist_id=args.therapist_id or default_therapist_id,
        days=args.days,
        top_k=args.top_k,
        raw_text="cli weekly-report",
        use_agent_sdk=use_agent_sdk,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        context={},
    )


def print_response(response, *, json_output: bool, show_trace: bool = False) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "success": response.success,
                    "task_type": response.task_type,
                    "execution_mode": response.execution_mode,
                    "llm_provider": response.llm_provider,
                    "llm_model": response.llm_model,
                    "structured_output": response.structured_output,
                    "final_text": response.final_text,
                    "validation_issues": response.validation_issues,
                    "execution_trace": [item.model_dump(mode="json") for item in response.execution_trace],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(
        f"[success={response.success}, "
        f"execution_mode={response.execution_mode}, "
        f"llm_provider={response.llm_provider}, "
        f"llm_model={response.llm_model or '默认'}]"
    )
    if response.task_type == OrchestrationTaskType.OPEN_ANALYTICS_QUERY.value and isinstance(response.structured_output, dict):
        subtype = response.structured_output.get("subtype")
        parse_mode = ((response.structured_output.get("query_plan") or {}).get("time_parse_mode"))
        if subtype or parse_mode:
            print(f"[open_analytics subtype={subtype or 'unclassified'} parse_mode={parse_mode or 'unknown'}]")
    print(response.final_text)
    if response.validation_issues:
        print("\n[校验问题]")
        for item in response.validation_issues:
            print(f"- {item}")
    if show_trace and response.execution_trace:
        print("\n[执行轨迹]")
        for item in response.execution_trace:
            status = "成功" if item.success else "失败"
            print(f"- {item.step_id} | {item.tool_name} | {status} | {item.output_summary}")


def execute_args(
    args: argparse.Namespace,
    *,
    orchestrator: RehabAgentOrchestrator | None = None,
    emit_output: bool = True,
):
    orchestrator = orchestrator or RehabAgentOrchestrator()
    request = build_request_from_args(args)
    response = orchestrator.run(request)
    if emit_output:
        print_response(response, json_output=args.json_output, show_trace=getattr(args, "show_trace", False))
    return response


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    actual_argv = list(argv or sys.argv[1:])
    args = parser.parse_args(actual_argv)
    args = apply_global_overrides_from_argv(args, actual_argv)
    execute_args(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
