"""
创建日期：2026-10-10
文件功能：默认分支——agent 自主选路 + 后处理，答复文本给前端。

默认分支走单 Agent 编排：agent 自主选择要访问的接口、拿到数据后，把
「答复文本 + 工具结果」交给 post_process 做特殊处理，最终把处理结果的
answer 字段文本（如 "[Answer:]..."）作为 answer_part.text 下发给前端。

graph / plan 是编排降级链（非 agent 模式才走），保持原样。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meta_agent.agent.results import collect_agent_results

from . import post_process as post_process_module

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext
    from meta_agent.application.service import ApplicationService


class Flow:
    """默认流程：agent 选路 + 后处理 → answer_part.text。"""

    def __init__(self, application: ApplicationService) -> None:
        self._application = application

    async def execute(self, ctx: RunContext) -> None:
        app = self._application
        if app.agent_runtime:
            # agent 只选路 + 调接口；答复文本作为返回值交给后处理，不直接下发。
            answer = await app.agent_runtime.execute(ctx, emit_answer=False)
            collected = await collect_agent_results(ctx)
            result = post_process_module.post_process(ctx, answer, collected)
            # 前端要的是后处理后的 answer 字段文本（"[Answer:]..."），不是整个 dict。
            text = result.get("answer") if isinstance(result, dict) else result
            await ctx.emitter.emit(
                "answer_part",
                {
                    "text": text,
                    "fact_ids": [],
                    "doc_refs": [],
                    "revision": 1,
                    "replaces": None,
                },
                goal_id="agent",
            )
        elif app.graph:
            await app._enrich_patient_brief(ctx)
            await app.graph.ainvoke(
                {"run_id": ctx.record.run_id, "stage": "accepted"},
                config={"configurable": {"thread_id": ctx.record.run_id}},
                context=ctx,
            )
        else:
            await app._enrich_patient_brief(ctx)
            await app.plan(ctx)
            await app.execute_plan(ctx)
