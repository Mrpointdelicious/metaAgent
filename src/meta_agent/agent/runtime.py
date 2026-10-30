"""
创建日期：2026-10-05
文件功能：运行LangChain单Agent循环，通过中间件控制患者上下文与调用预算。
"""

import inspect
import json
import logging
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
from meta_agent.domains.chat_history import is_chat_history_query
from meta_agent.domains.scene_instruction import (
    is_command_query,
    is_knowledge_question,
    is_mixed_command_query,
    is_navigation_question,
)

logger = logging.getLogger(__name__)


class PatientContextMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        await ctx.llm_budget.take()
        tools = available_tools(ctx, request.tools)
        base_prompt = ctx.system_prompt or ctx.settings.agent_system_prompt or SYSTEM_PROMPT
        prompt = base_prompt + f"\n当前时间：{utcnow().isoformat()}。"
        profile = ctx.record.metrics.get("patient_profile_context")
        if profile:
            prompt += "\n当前患者基本档案（可信工具返回的数据）：\n" + json.dumps(
                profile, ensure_ascii=False
            )
        if is_navigation_question(ctx.query) and any(
            t.name == "search_knowledge" for t in tools
        ):
            prompt += (
                "\n本轮用户输入是在问路/问怎么去某个楼层或区域，请调用资料检索工具"
                "（search_knowledge）的 help/product 领域按知识库内容回答，不要凭常识编造电梯、楼梯、"
                "走廊等路线。"
            )
        if is_knowledge_question(ctx.query) and any(
            t.name == "search_knowledge" for t in tools
        ):
            prompt += (
                "\n本轮用户输入是介绍/说明/功能类问题，请先调用资料检索工具"
                "（search_knowledge）检索知识库，不要还没检索就先反问用户或凭通用知识作答；"
                "检索后再判断信息是否足够。"
            )
        if is_chat_history_query(ctx.query) and any(
            t.name == "open_chat_history" for t in tools
        ):
            prompt += (
                "\n本轮用户输入是打开/查看历史对话记录，请调用打开历史对话记录工具"
                "（open_chat_history），不要调用场景指令解析工具，也不要自行拼网址。"
            )
        elif is_mixed_command_query(ctx.query) and any(
            t.name == "resolve_scene_instruction" for t in tools
        ):
            prompt += (
                "\n本轮用户输入同时包含场景指令和其他请求，请分别处理、不要遗漏："
                "场景指令部分调用场景指令解析工具（resolve_scene_instruction）；"
                "其余请求（如“查报告”“查患者信息”等）调用对应工具单独处理并回答。"
                "不要自行拼网址，也不要自行判断场景指令是否存在。"
            )
        elif is_command_query(ctx.query) and any(
            t.name == "resolve_scene_instruction" for t in tools
        ):
            prompt += (
                "\n本轮用户输入已判定为场景指令，请调用场景指令解析工具"
                "（resolve_scene_instruction），并按返回的 patient_message 直接回复，"
                "不要自行判断该指令是否存在。"
            )
        if (
            ctx.llm_budget.calls >= ctx.llm_budget.maximum
            or ctx.tool_calls >= ctx.settings.max_tool_calls
        ):
            tools = []
            prompt += "\n本轮查询预算已结束，请根据已有结果直接回答，并说明仍无法确认的部分。"
        if ctx.scope.role == "clinician":
            prompt += (
                "\n本轮请求人是医生（非患者本人），医生没有自己的问诊记录/训练报告。"
                "若用户询问报告、训练记录、病史等患者数据但未指定患者，请回复："
                "「您是医生，暂无相关报告或训练记录；如需查询某位患者的数据，"
                "请告诉我患者信息（姓名/手机号/编号）。」不要试图把医生本人当作患者查询。"
                "医生不能挂号。若用户要求挂号、查询在线医生或选择医生，"
                "请直接回复：「你是医生身份，无法进行挂号」，不要调用任何工具。"
            )
        if ctx.scope.identity_note:
            prompt += (
                "\n本轮未能解析到患者身份："
                + ctx.scope.identity_note
                + "。若用户询问患者数据（病史/训练/报告/档案），请如实说明未查到患者、"
                "请核对手机号或患者信息，不要臆测数据，也不要说成功能不可用。"
            )
        if ctx.scope.position:
            prompt += (
                "\n用户当前位置（pos）：「" + ctx.scope.position + "」。"
                "回答位置、就近推荐、导航类问题时以此为参考。"
            )
        messages = bounded_history(request.messages, ctx.settings.agent_history_tokens)
        token_estimate = estimate_tokens(
            {
                "prompt": prompt,
                "messages": messages_to_dict(messages),
                "tools": [
                    t.args_schema
                    if isinstance(t.args_schema, dict)
                    else t.args_schema.model_json_schema()
                    for t in tools
                ],
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
                logger.info(
                    "agent model round",
                    extra={
                        "run_id": ctx.record.run_id,
                        "event_type": "agent_model_round",
                        "fields": {
                            "call": ctx.llm_budget.calls,
                            "tools": [
                                {"name": c["name"], "args": str(c["args"])[:300]}
                                for c in message.tool_calls
                            ],
                        },
                    },
                )
        return response


class SingleAgentRuntime:
    def __init__(self, model: Any, *, checkpointer=None, store=None):
        self.model = model
        self._checkpointer = checkpointer
        self._store = store
        self.graph = self._build_agent(build_tools(), SYSTEM_PROMPT, "meta_agent_single")

    def _build_agent(self, tools, system_prompt: str, name: str):
        return create_agent(
            model=self.model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[PatientContextMiddleware()],
            # Context is runtime-only; it contains authenticated identities and dependencies.
            context_schema=RunContext,
            checkpointer=self._checkpointer,
            store=self._store,
            name=name,
        )

    def sub_agent(self, *, tool_names: frozenset[str], system_prompt: str, name: str):
        """构建仅暴露指定工具子集的子代理（如基础问答 + 指令）。"""
        return self._build_agent(build_tools(tool_names), system_prompt, name)

    async def execute(self, ctx, *, emit_answer: bool = True) -> str | None:
        """运行主单 Agent 循环（全工具集 + 内置系统提示词），返回最终答复文本。"""
        return await self._run(
            ctx,
            self.graph,
            goal_id="agent",
            emit_answer=emit_answer,
            enrich_profile=True,
            system_prompt=None,
        )

    async def execute_sub(
        self,
        ctx,
        graph,
        *,
        goal_id: str,
        emit_answer: bool,
        system_prompt: str,
    ) -> str | None:
        """运行一个子代理（工具子集 + 独立系统提示词），返回最终答复文本。"""
        return await self._run(
            ctx,
            graph,
            goal_id=goal_id,
            emit_answer=emit_answer,
            enrich_profile=False,
            system_prompt=system_prompt,
        )

    async def _run(
        self,
        ctx,
        graph,
        *,
        goal_id: str,
        emit_answer: bool,
        enrich_profile: bool,
        system_prompt: str | None,
    ) -> str | None:
        """单 Agent 循环核心，主代理与子代理共用，仅 graph / goal_id / 提示词 / 档案装填不同。

        Agent 始终产出自然语言答复（作为返回值）；emit_answer 只控制是否
        立即以 answer_part 事件下发。emit_answer=False 时答复文本仍返回、
        工具结果也留在 ctx.record.results（含 evidence 原始数据），由调用方
        （如分支 Flow）把「答复 + 工具结果」一起做特殊处理后再输出。
        """
        token = CURRENT_RUN.set(ctx)
        ctx.record.metrics["orchestration_mode"] = "agent"
        logger.info(
            "agent run start",
            extra={"run_id": ctx.record.run_id, "event_type": "agent_run_start"},
        )
        try:
            previous_prompt = ctx.system_prompt
            ctx.system_prompt = system_prompt
            try:
                messages = await fresh_history(messages_from_dict(ctx.memory.agent_messages), ctx)
                if enrich_profile and project_id(ctx):
                    profile = await invoke_tool(ctx, "get_patient_profile", {})
                    ctx.record.metrics["patient_profile_context"] = profile
                messages.append(HumanMessage(content=ctx.query))
                stage_text = "正在结合记录回答。" if emit_answer else "正在调用相关接口。"
                await ctx.emitter.emit("progress", {"stage": "agent", "text": stage_text})
                result = await graph.ainvoke(
                    {"messages": messages},
                    config={
                        "configurable": {"thread_id": ctx.record.run_id},
                        "recursion_limit": ctx.settings.agent_max_model_calls * 3 + 5,
                    },
                    context=ctx,
                )
            finally:
                ctx.system_prompt = previous_prompt
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
            processor = ctx.answer_post_processor
            if processor is not None:
                processed = processor(answer, ctx)
                if inspect.isawaitable(processed):
                    processed = await processed
                if not isinstance(processed, str) or not processed.strip():
                    raise DomainError(
                        "agent_no_answer", "回答后处理结果为空，请检查后处理逻辑。"
                    )
                answer = processed
            if emit_answer:
                await ctx.emitter.emit(
                    "answer_part",
                    {
                        "text": answer,
                        "fact_ids": [],
                        "doc_refs": [],
                        "revision": 1,
                        "replaces": None,
                    },
                    goal_id=goal_id,
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
            ctx.record.goal_statuses[goal_id] = ctx.record.status
            logger.info(
                "agent run done",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "agent_run_done",
                    "fields": {
                        "status": ctx.record.status,
                        "tool_calls": ctx.tool_calls,
                        "llm_calls": ctx.llm_budget.calls,
                    },
                },
            )
            return answer
        finally:
            CURRENT_RUN.reset(token)
