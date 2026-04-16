from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import RehabAgentOrchestrator
from config import get_settings

from Demo.cli import apply_global_overrides_from_argv, build_parser, build_request_from_args, print_response
from Demo.dialogue import (
    ConversationState,
    build_welcome,
    demo_sample_text,
    normalize_cli_tokens,
    parse_natural_language_request,
    summarize_context,
    update_state_from_response,
)


def _configure_console_encoding() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def main() -> int:
    _configure_console_encoding()
    settings = get_settings()
    parser = build_parser()
    orchestrator = RehabAgentOrchestrator(settings)
    if len(sys.argv) > 1:
        argv = sys.argv[1:]
        args = parser.parse_args(argv)
        args = apply_global_overrides_from_argv(args, argv)
        request = build_request_from_args(args)
        response = orchestrator.run(request)
        print_response(response, json_output=args.json_output, show_trace=getattr(args, "show_trace", False))
        return 0

    runtime_llm = {
        "llm_provider": None,
        "llm_model": None,
        "llm_base_url": None,
        "use_agent_sdk": None,
        "show_trace": False,
    }
    state = ConversationState()

    print(build_welcome(settings))
    while True:
        try:
            raw = input("rehab-demo> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        lower = raw.lower()
        if lower in {"exit", "quit"}:
            return 0
        if lower in {"help", "?"}:
            print(build_welcome(settings))
            continue
        if lower == "show-context":
            print(summarize_context(state, settings))
            continue
        if lower == "clear-context":
            state = ConversationState()
            print("会话上下文已清空。")
            continue
        if lower == "show-demo-sample":
            print(demo_sample_text(settings))
            continue
        if lower == "show-llm":
            resolved = settings.resolve_llm_config(
                provider=runtime_llm["llm_provider"],
                model=runtime_llm["llm_model"],
                base_url=runtime_llm["llm_base_url"],
            )
            agent_mode = "auto" if runtime_llm["use_agent_sdk"] is None else ("on" if runtime_llm["use_agent_sdk"] else "off")
            print(
                f"provider={resolved.provider} "
                f"model={resolved.model or '默认'} "
                f"base_url={resolved.base_url or '默认'} "
                f"agent_sdk={agent_mode} "
                f"trace={'开' if runtime_llm['show_trace'] else '关'}"
            )
            continue
        if lower == "clear-llm":
            runtime_llm = {
                "llm_provider": None,
                "llm_model": None,
                "llm_base_url": None,
                "use_agent_sdk": None,
                "show_trace": False,
            }
            print("LLM 运行时覆盖配置已清空。")
            continue
        if lower.startswith("set-provider "):
            provider = raw.split(maxsplit=1)[1].strip().lower()
            if provider not in {"openai", "qwen", "deepseek"}:
                print("不支持的 provider，请使用 openai、qwen 或 deepseek。")
                continue
            runtime_llm["llm_provider"] = provider
            print(f"LLM provider 已切换为 {provider}。")
            continue
        if lower.startswith("set-model "):
            runtime_llm["llm_model"] = raw.split(maxsplit=1)[1].strip()
            print(f"LLM 模型已切换为 {runtime_llm['llm_model']}。")
            continue
        if lower.startswith("set-base-url "):
            runtime_llm["llm_base_url"] = raw.split(maxsplit=1)[1].strip()
            print(f"LLM base URL 已切换为 {runtime_llm['llm_base_url']}。")
            continue
        if lower.startswith("set-agent "):
            mode = raw.split(maxsplit=1)[1].strip().lower()
            if mode not in {"on", "off"}:
                print("不支持的模式，请使用 set-agent on 或 set-agent off。")
                continue
            runtime_llm["use_agent_sdk"] = mode == "on"
            print(f"Agent SDK 模式已切换为 {mode}。")
            continue
        if lower.startswith("set-trace "):
            mode = raw.split(maxsplit=1)[1].strip().lower()
            if mode not in {"on", "off"}:
                print("不支持的模式，请使用 set-trace on 或 set-trace off。")
                continue
            runtime_llm["show_trace"] = mode == "on"
            print(f"执行轨迹显示已切换为 {mode}。")
            continue

        cli_tokens, notice = normalize_cli_tokens(raw)
        if notice:
            print(notice)
            continue
        if cli_tokens is not None:
            try:
                args = parser.parse_args(cli_tokens)
            except SystemExit:
                continue
            args = apply_global_overrides_from_argv(args, cli_tokens)
            if runtime_llm["llm_provider"] and not args.llm_provider:
                args.llm_provider = runtime_llm["llm_provider"]
            if runtime_llm["llm_model"] and not args.llm_model:
                args.llm_model = runtime_llm["llm_model"]
            if runtime_llm["llm_base_url"] and not args.llm_base_url:
                args.llm_base_url = runtime_llm["llm_base_url"]
            if runtime_llm["use_agent_sdk"] is not None:
                args.use_agent_sdk = runtime_llm["use_agent_sdk"]
            if runtime_llm["show_trace"] and not getattr(args, "show_trace", False):
                args.show_trace = True
            request = build_request_from_args(args)
            response = orchestrator.run(request)
            print_response(response, json_output=args.json_output, show_trace=args.show_trace)
            update_state_from_response(state, request, response)
            continue

        request, note = parse_natural_language_request(
            raw,
            state,
            settings,
            use_agent_sdk=runtime_llm["use_agent_sdk"],
            llm_provider=runtime_llm["llm_provider"],
            llm_model=runtime_llm["llm_model"],
            llm_base_url=runtime_llm["llm_base_url"],
        )
        if note:
            print(note)
        if request is None:
            continue
        response = orchestrator.run(request)
        print_response(response, json_output=False, show_trace=runtime_llm["show_trace"])
        update_state_from_response(state, request, response)


if __name__ == "__main__":
    raise SystemExit(main())
