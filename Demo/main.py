from __future__ import annotations

import shlex
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Demo.cli import build_parser, execute_args


WELCOME = """Rehab Execution Deviation Demo
Commands:
  review-patient --plan-id 6 --days 7
  review-patient --patient-id 544 --days 30
  screen-risk --therapist-id 1623 --days 30
  weekly-report --therapist-id 1623 --days 30

Runtime LLM switch:
  set-provider openai|qwen|deepseek
  set-model <model_name>
  set-base-url <url>
  show-llm
  clear-llm

Other:
  help
  exit
"""


def main() -> int:
    parser = build_parser()
    if len(sys.argv) > 1:
        args = parser.parse_args(sys.argv[1:])
        return execute_args(args)

    runtime_llm = {
        "llm_provider": None,
        "llm_model": None,
        "llm_base_url": None,
    }

    print(WELCOME)
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
            print(WELCOME)
            continue
        if lower == "show-llm":
            print(
                f"provider={runtime_llm['llm_provider'] or 'default'} "
                f"model={runtime_llm['llm_model'] or 'default'} "
                f"base_url={runtime_llm['llm_base_url'] or 'default'}"
            )
            continue
        if lower == "clear-llm":
            runtime_llm = {"llm_provider": None, "llm_model": None, "llm_base_url": None}
            print("LLM runtime overrides cleared.")
            continue
        if lower.startswith("set-provider "):
            provider = raw.split(maxsplit=1)[1].strip().lower()
            if provider not in {"openai", "qwen", "deepseek"}:
                print("Unsupported provider. Use openai, qwen, or deepseek.")
                continue
            runtime_llm["llm_provider"] = provider
            print(f"LLM provider set to {provider}.")
            continue
        if lower.startswith("set-model "):
            runtime_llm["llm_model"] = raw.split(maxsplit=1)[1].strip()
            print(f"LLM model set to {runtime_llm['llm_model']}.")
            continue
        if lower.startswith("set-base-url "):
            runtime_llm["llm_base_url"] = raw.split(maxsplit=1)[1].strip()
            print(f"LLM base URL set to {runtime_llm['llm_base_url']}.")
            continue

        try:
            args = parser.parse_args(shlex.split(raw))
            if runtime_llm["llm_provider"] and not args.llm_provider:
                args.llm_provider = runtime_llm["llm_provider"]
            if runtime_llm["llm_model"] and not args.llm_model:
                args.llm_model = runtime_llm["llm_model"]
            if runtime_llm["llm_base_url"] and not args.llm_base_url:
                args.llm_base_url = runtime_llm["llm_base_url"]
            execute_args(args)
        except SystemExit:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
