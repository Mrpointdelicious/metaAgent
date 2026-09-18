"""
创建日期：2026-09-12
文件功能：校验事实来源并用确定模板渐进回答，可选模型仅选择事实编号。
"""

import asyncio
import json
from typing import Any

from pydantic import Field

from meta_agent.application.context import RunContext
from meta_agent.context.budget import ContextSelector, estimate_tokens
from meta_agent.contracts import (
    DomainError,
    FactView,
    StrictModel,
    TaskResult,
    TaskSpec,
    fingerprint,
)


class FactSelection(StrictModel):
    fact_ids: list[str] = Field(max_length=100)


def safe_text(value: str) -> str:
    # These characters cannot become a legacy action/media frame in a downstream renderer.
    return value.replace("[", "［").replace("]", "］").replace("<", "＜").replace(">", "＞")


STATUS_TEXT = {
    "completed": "已完成",
    "not_started": "未开始",
    "not_completed": "未完成",
    "execution_unknown": "执行状态未知",
    "voided": "已作废",
    "missing": "缺失",
    "unknown": "未知",
    "invalid": "无效",
    "not_comparable": "不可比较",
}


def render_fact(view: FactView) -> str:
    fact = view.fact
    label = safe_text(view.label)
    if fact.value is None:
        value = "缺失" if fact.value_status == "missing" else "未知"
    elif isinstance(fact.value, bool):
        value = "是" if fact.value else "否"
    else:
        value = safe_text(str(fact.value))
        if fact.semantic_key == "training_state":
            value = STATUS_TEXT.get(value, value)
    if fact.unit == "unknown":
        value += "（单位未知）"
    elif fact.unit:
        value += " " + safe_text(fact.unit)
    if fact.value_status not in {"valid", "missing"}:
        value += "（" + STATUS_TEXT[fact.value_status] + "）"
    return f"{label}：{value}"


class ResponseComposer:
    def __init__(self, model: Any = None) -> None:
        self.model = (
            model.with_structured_output(FactSelection, include_raw=True) if model else None
        )
        self.selector = ContextSelector()

    async def on_result(self, task: TaskSpec, result: TaskResult, ctx: RunContext) -> None:
        if result.status == "clarification":
            await ctx.emitter.emit(
                "clarification",
                {"text": safe_text(result.message), "missing_slots": []},
                task_id=task.task_id,
                goal_id=task.goal_ids[0],
            )
        elif result.status in {
            "failed",
            "unavailable",
            "unsupported",
            "blocked_dependency",
            "cancelled",
        }:
            await ctx.emitter.emit(
                "task_failed",
                {
                    "text": safe_text(result.message),
                    "code": result.code or result.status,
                    "retryable": result.retryable,
                },
                task_id=task.task_id,
                goal_id=task.goal_ids[0] if len(task.goal_ids) == 1 else None,
            )
        artifact = result.outputs.get("artifact")
        if artifact and artifact["artifact_ref"] not in ctx.artifact_refs:
            await ctx.emitter.emit(
                "artifact_ready", artifact, task_id=task.task_id, goal_id=task.goal_ids[0]
            )
            ctx.artifact_refs.add(artifact["artifact_ref"])
        if task.capability.startswith("scene."):
            return
        for gid in task.goal_ids:
            if gid not in ctx.goals:
                continue
            goal = ctx.goals[gid]
            # A successful record lookup is an internal step for an artifact-only request.
            if goal.output == "artifact" and result.status == "succeeded":
                continue
            await self.answer_goal(gid, ctx)

    async def answer_goal(self, gid: str, ctx: RunContext) -> None:
        tasks = [
            t
            for t in ctx.record.plan.tasks
            if gid in t.goal_ids and t.task_id in ctx.record.results
        ]
        has_session = any(
            t.capability == "rehab.session"
            and ctx.record.results[t.task_id].status in {"succeeded", "partial"}
            for t in tasks
        )
        relevant = [
            ctx.record.results[t.task_id]
            for t in tasks
            if not (has_session and t.capability == "rehab.resolve_session")
        ]
        views = [view for result in relevant for view in result.facts]
        views = [
            v
            for v in views
            if "display" in v.fact.allowed_uses
            and (v.fact.audience in {"both", "patient"} or ctx.scope.role == "clinician")
        ]
        if not views:
            messages = list(dict.fromkeys(r.message for r in relevant if r.message))
            if messages:
                await self.emit_answer(gid, "\n".join(map(safe_text, messages)), [], [], ctx)
            return
        if not await ctx.repository.verify_facts(ctx.scope.scope_hash, views):
            raise DomainError("fact_source_mismatch", "事实与授权来源不一致，已停止输出。")
        goal = ctx.goals[gid]
        goal_count = max(1, sum(g.kind != "scene_action" for g in ctx.goals.values()))
        limit = (
            min(ctx.settings.answer_input_tokens, ctx.settings.model_context_tokens - 2048 - 512)
            // goal_count
        )
        overhead = {
            "instruction": "只选择相关事实编号，禁止创建或改写事实。",
            "schema": FactSelection.model_json_schema(),
        }
        selected = self.selector.select(goal.query_span, {gid: views}, limit, overhead)[gid]
        # Selection is optional. Templates still work if model budget/provider fails.
        if self.model and ctx.llm_budget.calls < ctx.llm_budget.maximum:
            messages = [
                ("system", overhead["instruction"]),
                (
                    "human",
                    json.dumps(
                        {
                            "query": goal.query_span,
                            "facts": [v.model_dump(mode="json") for v in selected],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ]
            if estimate_tokens(messages) + estimate_tokens(overhead["schema"]) <= limit:
                try:
                    await ctx.llm_budget.take()
                    async with asyncio.timeout(
                        min(ctx.settings.answer_timeout_seconds, ctx.remaining)
                    ):
                        answer = await self.model.ainvoke(messages)
                    if isinstance(answer, dict) and "parsed" in answer:
                        usage = getattr(answer.get("raw"), "usage_metadata", None)
                        if usage:
                            ctx.llm_budget.usage.append(dict(usage))
                        answer = answer["parsed"]
                    choice = FactSelection.model_validate(answer)
                    known = {v.fact.fact_id for v in selected}
                    if set(choice.fact_ids) <= known:
                        selected = [
                            v for v in selected if v.required or v.fact.fact_id in choice.fact_ids
                        ]
                except Exception:
                    pass
        refs = list(
            dict.fromkeys(ref for result in relevant for ref in result.outputs.get("doc_refs", []))
        )
        text = "\n".join(render_fact(view) for view in selected)
        if refs:
            text += "\n资料章节：" + "；".join(map(safe_text, refs))
        messages = list(dict.fromkeys(r.message for r in relevant if r.message))
        if messages:
            text += "\n" + "\n".join(map(safe_text, messages))
        ctx.record.metrics.setdefault("answer_context_estimated_tokens", {})[gid] = estimate_tokens(
            {
                "query": goal.query_span,
                "facts": [v.model_dump(mode="json") for v in selected],
                "wrapper": overhead,
            }
        )
        await self.emit_answer(gid, text, [v.fact.fact_id for v in selected], refs, ctx)

    async def emit_answer(
        self, gid: str, text: str, facts: list[str], refs: list[str], ctx: RunContext
    ) -> None:
        digest = fingerprint([text, facts, refs])
        if ctx.answer_fingerprints.get(gid) == digest:
            return
        previous = ctx.answer_events.get(gid)
        event = await ctx.emitter.emit(
            "answer_part",
            {
                "text": text,
                "fact_ids": facts,
                "doc_refs": refs,
                "revision": previous.payload["revision"] + 1 if previous else 1,
                "replaces": previous.event_id if previous else None,
            },
            goal_id=gid,
        )
        ctx.answer_fingerprints[gid], ctx.answer_events[gid] = digest, event
