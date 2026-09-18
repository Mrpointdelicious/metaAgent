"""
创建日期：2026-09-18
文件功能：IReGo 固定工作流 Eval——确定性 dry-run 与真实后端两段式指标采集。
运行：uv run python tests/irego_eval.py [--real]
"""

import argparse
import asyncio
import json
import time

from meta_agent.application.service import ApplicationRequest
from meta_agent.contracts import IReGoRequest
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import report_response, runtime

CASES = [
    ("解读最近训练并生成报告图片", "session", "latest_record", None, True),
    ("为我最近一次训练生成报告图片", "session", "latest_record", None, True),
    ("不要生成报告，只解释最近训练", "session", "latest_record", None, False),
    ("最近4次训练趋势并出图", "trend", "latest_count", 4, True),
    ("查看患者概况", "overview", "latest_record", None, False),
    ("查看训练历史", "history", "latest_record", None, False),
]

DEPENDENCY_MARKS = ("任务依赖存在循环或缺失", "目标依赖无法解析", "invalid_dependency")


def evaluate(record, expected, elapsed):
    metrics = {
        "latency_ms": round(elapsed * 1000, 1),
        "e2e_outcome": record.status,
        "dependency_error_count": 0,
        "compile_success": False,
        "intent_parse_success": False,
        "operation_accuracy": False,
        "selector_accuracy": False,
        "artifact_intent_accuracy": False,
        "tool_execution_success": False,
        "fact_projection_success": False,
        "artifact_success": False,
    }
    text = " ".join(e.payload.get("text", "") for e in record.events)
    metrics["dependency_error_count"] = sum(mark in text for mark in DEPENDENCY_MARKS)
    metrics["dependency_error_count"] += sum(
        r.code == "invalid_dependency" for r in record.results.values()
    )
    if record.decision and record.decision.decision == "execute":
        metrics["intent_parse_success"] = True
    if record.plan and record.plan.tasks:
        metrics["compile_success"] = True
        task = record.plan.tasks[0]
        if task.capability == "irego.execute":
            request = IReGoRequest.model_validate(task.arguments)
            operation, mode, count, artifact = expected
            metrics["operation_accuracy"] = request.operation == operation
            metrics["selector_accuracy"] = (
                request.selector.mode == mode and request.selector.count == count
            )
            metrics["artifact_intent_accuracy"] = request.need_artifact == artifact
    results = list(record.results.values())
    if results and any(r.status in {"succeeded", "partial"} for r in results):
        metrics["tool_execution_success"] = True
    if results and any(r.facts for r in results):
        metrics["fact_projection_success"] = True
    metrics["artifact_success"] = any(e.type == "artifact_ready" for e in record.events)
    return metrics


async def run_case(application, backend, query, expected, request_id, patient="461"):
    started = time.monotonic()
    run = await application.execute(
        ApplicationRequest(
            query,
            TrustedScope("tenant", "actor", patient, space_id="space", scene_version=7),
            f"c-{request_id}",
            request_id,
        )
    )
    return evaluate(run.record, expected, time.monotonic() - started)


async def dry_run_eval():
    rows = []
    async with runtime() as (c, backend):
        backend.overrides["generate_irego_single_session_report"] = report_response
        backend.overrides["generate_irego_longitudinal_report"] = report_response
        for i, (query, *expected) in enumerate(CASES):
            metrics = await run_case(c.application, backend, query, tuple(expected), f"eval-a{i}")
            rows.append({"case": query, "expected": tuple(expected), **metrics})
        # Case G：复杂查询连续 10 次
        g_metrics = []
        for i in range(10):
            metrics = await run_case(
                c.application,
                backend,
                "解读最近训练并生成报告图片",
                ("session", "latest_record", None, True),
                f"eval-g{i}",
            )
            g_metrics.append(metrics)
        rows.append(
            {
                "case": "Case G x10",
                "expected": ("session", "latest_record", None, True),
                "runs": g_metrics,
                "dependency_error_count": sum(m["dependency_error_count"] for m in g_metrics),
            }
        )
    return rows


async def real_backend_eval():
    from meta_agent.config import Settings
    from meta_agent.infrastructure.container import create_container

    rows = []
    container = await create_container(Settings())
    try:
        for i, (query, *expected) in enumerate(CASES[:3] + [CASES[4]]):
            metrics = await run_case(
                container.application, None, query, tuple(expected), f"real-a{i}", patient="3892"
            )
            rows.append({"case": query, "expected": tuple(expected), **metrics})
    finally:
        await container.close()
    return rows


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    output = {"mode": "dry-run-deterministic", "rows": await dry_run_eval()}
    if args.real:
        output["real_backend"] = await real_backend_eval()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
