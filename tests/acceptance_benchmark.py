"""
创建日期：2026-09-12
文件功能：合成固定工具延迟下比较调度与原生事件队列耗时，不接真实患者或语音。
"""

import asyncio
import json
import math
import platform
import time
from pathlib import Path

from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import report_response, request, runtime, scene_response


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


async def measure(parallel, concurrency):
    async with runtime(scene_actions_enabled=True, max_parallel_tasks=parallel) as (c, backend):
        backend.overrides["navigate_scene"] = scene_response
        backend.overrides["generate_irego_single_session_report"] = report_response
        backend.delays.update(
            navigate_scene=0.02,
            get_multisource_patient_context=0.08,
            get_irego_patient_history=0.03,
            get_irego_session_analysis=0.03,
            generate_irego_single_session_report=0.25,
        )

        async def one(i):
            start = time.perf_counter()
            scope = TrustedScope("synthetic", f"actor-{i}", "1", space_id="space", scene_version=7)
            run = await c.application.start(
                request("打开面板，查询患者信息，解读最近训练并生成报告图片", f"r-{i}", scope=scope)
            )
            elapsed = {}
            while True:
                event = await run.emitter.queue.get()
                if event is None:
                    break
                elapsed.setdefault(event.type, (time.perf_counter() - start) * 1000)
                await run.emitter.mark_dispatched(event)
            await run.task
            assert run.record.status == "succeeded", run.record.goal_statuses
            return elapsed

        samples = await asyncio.gather(*[one(i) for i in range(concurrency)])
        return {
            "parallel_tasks": parallel,
            "concurrency": concurrency,
            "samples": len(samples),
            "p95_ms": {
                event: round(percentile([s[event] for s in samples], 0.95), 2)
                for event in (
                    "accepted",
                    "action_ready",
                    "answer_part",
                    "artifact_ready",
                    "completed",
                )
            },
        }


async def main():
    results = [await measure(parallel, n) for parallel in (1, 4) for n in (1, 5, 20)]
    output = {
        "date": "2026-09-12",
        "environment": platform.platform(),
        "python": platform.python_version(),
        "measurement": (
            "application event queue; in-memory Store; synthetic tools; cold per-scope caches"
        ),
        "tool_delay_seconds": {
            "scene": 0.02,
            "overview": 0.08,
            "history": 0.03,
            "session": 0.03,
            "report": 0.25,
        },
        "global_tool_concurrency": 8,
        "real_llm": False,
        "real_backend": False,
        "tts": False,
        "results": results,
    }
    target = Path(__file__).resolve().parents[1] / "docs/implementation-v1/模拟时延结果.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
