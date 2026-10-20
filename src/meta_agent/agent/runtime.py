"""
创建日期：2026-10-05
文件功能：运行LangChain单Agent循环，通过中间件控制患者上下文与调用预算。
"""

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict

from meta_agent.agent.context import bounded_history, fresh_history
from meta_agent.agent.tools import (
    CURRENT_RUN,
    available_tools,
    build_tools,
    invoke_tool,
    project_id,
)
from meta_agent.application.context import RunContext
from meta_agent.application.frontend_context import conversational_only, validate_natural_answer
from meta_agent.context.budget import estimate_tokens
from meta_agent.contracts import DomainError, fingerprint, utcnow
from meta_agent.events.publisher import AnswerContent
from meta_agent.prompts.contracts import AgentPromptInputs, FrontendPromptInputs


class PatientContextMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        await ctx.llm_budget.take()
        tools = available_tools(ctx, request.tools)
        budget_exhausted = (
            ctx.llm_budget.calls >= ctx.llm_budget.maximum
            or ctx.tool_calls >= ctx.settings.max_tool_calls
        )
        if budget_exhausted or (ctx.frontend_context and conversational_only(ctx.query)):
            tools = []
        rendered = ctx.prompts.render(
            ctx.prompt_binding,
            "rehab.agent",
            AgentPromptInputs(
                now=utcnow(),
                patient_profile=ctx.patient_profile_context,
                budget_exhausted=budget_exhausted,
            ),
        )
        prompt = rendered.system_message.content
        messages = bounded_history(request.messages, ctx.settings.agent_history_tokens)
        frontend = None
        if ctx.frontend_context:
            frontend = ctx.prompts.render(
                ctx.prompt_binding,
                "frontend.context",
                FrontendPromptInputs(
                    selected_session_ref=ctx.frontend_context.get("selected_session_ref"),
                    selected_record_domain=ctx.frontend_context.get("selected_record_domain"),
                    current_view=ctx.frontend_context.get("current_view"),
                    current_record=ctx.memory.current_record.model_dump(mode="json")
                    if ctx.memory.current_record
                    else None,
                ),
            )
            prompt += "\n" + frontend.messages[0][1] + "\n" + frontend.messages[1][1]
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
        ctx.record.metrics.setdefault("prompt_usages", []).append(
            rendered.usage(ctx.llm_budget.calls)
        )
        if frontend:
            ctx.record.metrics["prompt_usages"].append(frontend.usage(ctx.llm_budget.calls))
        ctx.record.metrics.setdefault("agent_tool_sets", []).append(
            {
                "call": ctx.llm_budget.calls,
                "names": [tool.name for tool in tools],
                "hash": fingerprint(
                    [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "schema": tool.args_schema.model_json_schema(),
                        }
                        for tool in tools
                    ]
                ),
            }
        )
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


# 单AgentRuntime管理


class SingleAgentRuntime:
    # 各类参数信息填充 创建由 LangChain 管理模型与工具循环的 Agent 图
    def __init__(self, model: Any, *, checkpointer=None, store=None):
        self.graph = create_agent(
            model=model,
            tools=build_tools(),
            middleware=[PatientContextMiddleware()],
            # Context is runtime-only; it contains authenticated identities and dependencies.
            context_schema=RunContext,
            checkpointer=checkpointer,
            store=store,
            name="meta_agent_single",
        )

    # 执行阶段
    async def execute(self, ctx):
        # 获取上下文等运行时属性，并进行绑定
        token = CURRENT_RUN.set(ctx)
        ctx.record.metrics["orchestration_mode"] = "agent"
        try:
            # 更新历史信息，message装填；fresh_history 检查历史工具消息中的 Evidence 是否仍然可用。
            # 如果证据过期，则修改模型可见的旧工具消息，标记需要重新查询。
            # 这里具体怎么组织的还可以详细看看
            messages = await fresh_history(messages_from_dict(ctx.memory.agent_messages), ctx)
            # 预填入患者基本信息
            if project_id(ctx) and not conversational_only(ctx.query):
                profile = await invoke_tool(ctx, "get_patient_profile", {})
                ctx.patient_profile_context = profile
            # 填入用户问题
            messages.append(HumanMessage(content=ctx.query))
            # 向前端发送报文
            if not (ctx.frontend_context and conversational_only(ctx.query)):
                await ctx.events.progress("agent")
            # 等待返回信息，如果有工具调用则执行工具调用
            result = await self.graph.ainvoke(
                {"messages": messages},
                config={
                    "configurable": {"thread_id": ctx.record.run_id},
                    "recursion_limit": ctx.settings.agent_max_model_calls * 3 + 5,
                },
                context=ctx,
            )
            # 更新历史记录
            history = result["messages"]
            # 筛选,但是为什么要做这么一层兜底？
            """
            reversed(history)：从最新消息开始向前查找。
            isinstance(m, AIMessage)：只考虑助手消息。
            not m.tool_calls：排除仍然要求调用工具的助手消息。
            next(..., None)：找到第一个符合条件的消息；如果没有则返回 None。
            """
            final = next(
                (m for m in reversed(history) if isinstance(m, AIMessage) and not m.tool_calls),
                None,
            )
            # 错误兜底
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
            if ctx.frontend_context:
                validate_natural_answer(answer)
            # 发送回答报文
            await ctx.events.answer(
                AnswerContent(text=answer, fact_ids=[], doc_refs=[]), goal_id="agent"
            )
            # The natural answer is retained alongside complete tool-call pairs, not a fixed plan.
            # 记忆更新
            ctx.memory.agent_messages = messages_to_dict(
                bounded_history(history, ctx.settings.agent_history_tokens)
            )
            latest = {}
            # 返回数据情况：根据工具名称保存各工具的最新状态。
            for task in ctx.record.results.values():
                latest[task.outputs.get("tool_name", task.task_id)] = task.status
            good = any(s in {"succeeded", "partial"} for s in latest.values())
            bad = any(s not in {"succeeded", "partial"} for s in latest.values())
            ctx.record.status = "partial" if good and bad else "unavailable" if bad else "succeeded"
            if any(s == "partial" for s in latest.values()):
                ctx.record.status = "partial"
            ctx.record.goal_statuses["agent"] = ctx.record.status
        finally:
            # 重置，为什么要重置？：恢复之前的上下文变量状态，
            # 避免当前执行结束后仍保留这次绑定。不太懂
            CURRENT_RUN.reset(token)
