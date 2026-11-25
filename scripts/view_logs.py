"""把 meta_agent 的 JSON 日志渲染成人能读懂的请求流水。

用法：
    python scripts/view_logs.py .tmp/lab_logs/meta_agent.log          # 看全部
    python scripts/view_logs.py .tmp/lab_logs/meta_agent.log --only-summary  # 只看请求摘要
    python scripts/view_logs.py .tmp/lab_logs/meta_agent.log --client primary  # 按令牌过滤
    python scripts/view_logs.py .tmp/lab_logs/meta_agent.log --tail 10        # 最近 10 条

默认每条日志打印：时间 / 级别 / 令牌 / flow / 状态 / query / 入参 / 回复。
颜色输出可关（--no-color），便于重定向到文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

LEVEL_TAG = {
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "DEBUG": "调试",
}


def _fmt(text: Any, limit: int = 0) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\n", " ⏎ ")
    if limit and len(value) > limit:
        return value[:limit] + "…"
    return value


def render(entry: dict[str, Any], *, color: bool = True) -> str:
    ts = _fmt(entry.get("timestamp"))
    level = _fmt(entry.get("level"))
    logger = _fmt(entry.get("logger", "")).replace("meta_agent.", "")
    message = _fmt(entry.get("message"))
    run_id = _fmt(entry.get("run_id"))
    event = _fmt(entry.get("event_type"))

    head = f"[{ts}] {LEVEL_TAG.get(level, level):<4} {logger}: {message}"
    if run_id:
        head += f"  run={run_id[:20]}"
    if event and event != message:
        head += f"  event={event}"

    lines = [head]
    client = entry.get("client")
    flow = entry.get("flow")
    status = entry.get("status")
    if client:
        lines.append(f"    令牌: {_fmt(client)}   flow: {_fmt(flow)}   状态: {_fmt(status)}")

    query = entry.get("query")
    if query:
        lines.append(f"    问: {_fmt(query)}")
    inputs = entry.get("inputs")
    if inputs:
        lines.append(f"    入参: {_fmt(inputs)}")
    answer = entry.get("answer")
    if answer:
        lines.append(f"    答: {_fmt(answer)}")
    error = entry.get("error_code")
    if error:
        lines.append(f"    错误: {_fmt(error)} {_fmt(entry.get('error_message'))}")
    elapsed = entry.get("elapsed_ms")
    if elapsed is not None:
        lines.append(
            f"    耗时: {elapsed:.0f}ms  工具: {entry.get('tool_calls', '-')}  "
            f"模型: {entry.get('llm_calls', '-')}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="把 meta_agent JSON 日志渲染成可读请求流水")
    parser.add_argument("logfile", nargs="?", default=".tmp/lab_logs/meta_agent.log",
                        help="日志文件路径（默认 .tmp/lab_logs/meta_agent.log）")
    parser.add_argument("--only-summary", action="store_true", help="只显示 request_summary")
    parser.add_argument("--client", default="", help="按 client 过滤")
    parser.add_argument("--tail", type=int, default=0, help="只显示最后 N 条")
    parser.add_argument("--no-color", action="store_true", help="保留参数兼容（当前无颜色输出）")
    args = parser.parse_args()

    path = Path(args.logfile)
    if not path.exists():
        print(f"日志文件不存在: {path}", file=sys.stderr)
        raise SystemExit(1)

    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.only_summary and entry.get("event_type") != "request_summary":
            continue
        if args.client and entry.get("client") != args.client:
            continue
        entries.append(entry)

    if args.tail:
        entries = entries[-args.tail:]

    for entry in entries:
        print(render(entry, color=not args.no_color))
        print("-" * 60)


if __name__ == "__main__":
    main()
