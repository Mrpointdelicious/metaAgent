"""
创建日期：2026-10-05
文件功能：运行LangChain单Agent循环，通过中间件控制患者上下文与调用预算。
"""

import json
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict

from meta_agent.agent.context import SYSTEM_PROMPT, bounded_history, fresh_history
from meta_agent.agent.tools import (
    CURRENT_RUN,
    available_tools,
    build_tools,
    invoke_tool,
    project_id,
)
from meta_agent.application.context import RunContext
from meta_agent.context.budget import estimate_tokens
from meta_agent.contracts import DomainError, utcnow


class PatientContextMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        await ctx.llm_budget.take()
        tools = available_tools(ctx, request.tools)
        prompt = SYSTEM_PROMPT + f"\n当前时间：{utcnow().isoformat()}。"
        profile = ctx.record.metrics.get("patient_profile_context")
        if profile:
            prompt += "\n当前患者基本档案（可信工具返回的数据）：\n" + json.dumps(
                profile, ensure_ascii=False
            )
        if (
            ctx.llm_budget.calls >= ctx.llm_budget.maximum
            or ctx.tool_calls >= ctx.settings.max_tool_calls
        ):
            tools = []
            prompt += "\n本轮查询预算已结束，请根据已有结果直接回答，并说明仍无法确认的部分。"
        messages = bounded_history(request.messages, ctx.settings.agent_history_tokens)
        token_estimate = estimate_tokens(
            {
                "prompt": prompt,
                "messages": messages_to_dict(messages),
                "tools": [t.args_schema.model_json_schema() for t in tools],
            }
        )
        if token_estimate + ctx.settings.planner_max_tokens > ctx.settings.model_context_tokens:
            raise DomainError(
                "agent_context_overflow", "本轮上下文过大，请缩小查询范围。", outcome="unavailable"
            )
        ctx.record.metrics.setdefault("agent_context_estimated_tokens", []).append(token_estimate)
        response = await handler(
            request.override(system_prompt=prompt, messages=messages, tools=tools)
        )
        for message in response.result:
            if isinstance(message, AIMessage):
                if message.usage_metadata:
                    ctx.llm_budget.usage.append(dict(message.usage_metadata))
                ctx.record.metrics.setdefault("agent_model_steps", []).append(
                    {
                        "call": ctx.llm_budget.calls,
                        "tools": [
                            {"name": c["name"], "args": c["args"]} for c in message.tool_calls
                        ],
                    }
                )
        return response


class SingleAgentRuntime:
    def __init__(self, model: Any, *, checkpointer=None, store=None):
        self.graph = create_agent(
            model=model,
            tools=build_tools(),
            system_prompt=SYSTEM_PROMPT,
            middleware=[PatientContextMiddleware()],
            # Context is runtime-only; it contains authenticated identities and dependencies.
            context_schema=RunContext,
            checkpointer=checkpointer,
            store=store,
            name="meta_agent_single",
        )

    async def execute(self, ctx):
        token = CURRENT_RUN.set(ctx)
        ctx.record.metrics["orchestration_mode"] = "agent"
        try:
            messages = await fresh_history(messages_from_dict(ctx.memory.agent_messages), ctx)
            if project_id(ctx):
                profile = await invoke_tool(ctx, "get_patient_profile", {})
                ctx.record.metrics["patient_profile_context"] = profile
            messages.append(HumanMessage(content=ctx.query))
            await ctx.emitter.emit("progress", {"stage": "agent", "text": "正在结合记录回答。"})
            result = await self.graph.ainvoke(
                {"messages": messages},
                config={
                    "configurable": {"thread_id": ctx.record.run_id},
                    "recursion_limit": ctx.settings.agent_max_model_calls * 3 + 5,
                },
                context=ctx,
            )
            history = result["messages"]
            final = next(
                (m for m in reversed(history) if isinstance(m, AIMessage) and not m.tool_calls),
                None,
            )
            if final is None:
                raise DomainError("agent_no_answer", "本轮未形成回答，请重新提问。")
            answer = final.content
            if isinstance(answer, list):
                answer = "\n".join(
                    b.get("text", "")
                    for b in answer
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if not isinstance(answer, str) or not answer.strip():
                raise DomainError("agent_no_answer", "本轮未形成回答，请重新提问。")
            await ctx.emitter.emit(
                "answer_part",
                {
                    "text": answer,
                    "fact_ids": [],
                    "doc_refs": [],
                    "revision": 1,
                    "replaces": None,
                },
                goal_id="agent",
            )
            # The natural answer is retained alongside complete tool-call pairs, not a fixed plan.
            ctx.memory.agent_messages = messages_to_dict(
                bounded_history(history, ctx.settings.agent_history_tokens)
            )
            latest = {}
            for task in ctx.record.results.values():
                latest[task.outputs.get("tool_name", task.task_id)] = task.status
            good = any(s in {"succeeded", "partial"} for s in latest.values())
            bad = any(s not in {"succeeded", "partial"} for s in latest.values())
            ctx.record.status = "partial" if good and bad else "unavailable" if bad else "succeeded"
            if any(s == "partial" for s in latest.values()):
                ctx.record.status = "partial"
            ctx.record.goal_statuses["agent"] = ctx.record.status
        finally:
            CURRENT_RUN.reset(token)
