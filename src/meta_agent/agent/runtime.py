"""
创建日期：2026-10-05
文件功能：运行LangChain单Agent循环，通过中间件控制患者上下文与调用预算。
"""

import inspect
import logging
import re
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict

from meta_agent.adapters.text import model_text
from meta_agent.agent.context import bounded_history, fresh_history
from meta_agent.agent.role_context import model_context
from meta_agent.agent.tools import (
    CURRENT_RUN,
    available_tools,
    build_tools,
    invoke_tool,
    tool_allowed,
)
from meta_agent.application.context import RunContext
from meta_agent.context.budget import estimate_tokens
from meta_agent.contracts import DomainError, utcnow
from meta_agent.domains.business_routing import BusinessRoute, classify_business_query
from meta_agent.domains.chat_history import is_chat_history_query
from meta_agent.domains.scene_instruction import (
    is_knowledge_question,
    is_navigation_question,
)
from meta_agent.memory.schemas import TOOL_NAME as MEMORY_TOOL_NAME
from meta_agent.memory.service import build_memory_tool, tool_result

logger = logging.getLogger(__name__)


class RoleContextMiddleware(AgentMiddleware):
    def __init__(self, memory_service=None):
        self.memory_service = memory_service

    async def awrap_tool_call(self, request, handler):
        ctx = request.runtime.context
        if request.tool_call["name"] == MEMORY_TOOL_NAME:
            if self.memory_service is None or not tool_allowed(ctx, MEMORY_TOOL_NAME):
                return tool_result(request, "memory_not_available")
            return await self.memory_service.wrap_tool_call(request, handler)
        if (
            ctx.settings.knowledge_sources
            and request.tool is not None
            and request.tool.name == "search_knowledge"
        ):
            selected = available_tools(ctx, [request.tool])
            if selected:
                request = request.override(tool=selected[0])
        return await handler(request)

    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        await ctx.llm_budget.take()
        tools = available_tools(ctx, request.tools)
        profile = ctx.profile
        route = classify_business_query(ctx.query)
        if "patient.read" not in profile.capabilities:
            route = BusinessRoute(
                scene=route.scene and "scene.act" in profile.capabilities,
                history=route.history and "history.open" in profile.capabilities,
                hospital_ops=route.hospital_ops and "hospital.metrics" in profile.capabilities,
            )
        if route.scene:
            # A preview can never satisfy an explicit request to execute an action.
            tools = [tool for tool in tools if tool.name != "resolve_scene_instruction"]
        base_prompt, messages = await model_context(ctx, request.messages, self.memory_service)
        prompt = base_prompt + f"\n当前时间：{utcnow().isoformat()}。"
        knowledge_tool = next((t.name for t in tools if t.name == "search_knowledge"), None)
        if is_navigation_question(ctx.query) and knowledge_tool:
            domain = "的 product 领域（场景库）" if knowledge_tool == "search_knowledge" else ""
            prompt += (
                "\n本轮用户输入是在问路/问怎么去某个楼层或区域，请调用资料检索工具"
                f"（{knowledge_tool}）{domain}按知识库内容回答，不要凭常识编造电梯、楼梯、"
                "走廊等路线。"
            )
        if is_knowledge_question(ctx.query) and knowledge_tool:
            prompt += (
                "\n本轮用户输入是介绍/说明/功能类问题，请先调用资料检索工具"
                f"（{knowledge_tool}）检索知识库，不要还没检索就先反问用户或凭通用知识作答；"
                "检索后再判断信息是否足够。"
                "涉及楼层、科室、布局、场景、医院环境介绍时，必须选 product 领域"
                "（场景库 scene_kb），不要选 help。"
            )
        if is_chat_history_query(ctx.query) and any(t.name == "open_chat_history" for t in tools):
            prompt += (
                "\n本轮用户输入是打开/查看历史对话记录，请调用打开历史对话记录工具"
                "（open_chat_history），不要调用场景指令解析工具，也不要自行拼网址。"
            )
        # 路由提示必须对所有分支生效：患者数据查询、历史对话等非场景路由同样需要
        # 点名工具，否则模型没有落点，会直接自由作答（实测 steps 0）。
        route_hint = route.guidance()
        if route_hint:
            prompt += "\n" + route_hint
        if route.hospital_ops:
            if any(t.name == "query_hospital_operations" for t in tools):
                hint = (
                    "\n本轮要查医院运营数据：先用机构目录工具（query_hospital_directory）"
                    "确认机构编号，再调用 query_hospital_operations；机构不明确时先问清是哪家医院，"
                    "不要凭常识或场景描述作答。"
                )
                if not any(t.name == "query_hospital_directory" for t in tools):
                    hint = (
                        "\n本轮要查医院运营数据，请调用查询医院运营数据工具"
                        "（query_hospital_operations），不要凭常识或场景描述作答。"
                    )
                prompt += hint
            else:
                prompt += (
                    "\n本轮要查医院运营数据，但当前配置未启用该工具：直接说明暂时无法查询运营数据，"
                    "不编造数字，也不指引去某楼层看大屏。"
                )
        if route.scene:
            if any(t.name == "navigate_scene" for t in tools):
                prompt += "\n将本轮原话中的动作片段作为target；场景版本由服务端解析，无需追问用户。"
            else:
                prompt += "\n本轮场景执行工具未启用，请如实说明无法下发动作，不把预览当作执行。"
        if (
            ctx.llm_budget.calls >= ctx.llm_budget.maximum
            or ctx.tool_calls >= ctx.settings.max_tool_calls
        ):
            tools = []
            prompt += "\n本轮查询预算已结束，请根据已有结果直接回答，并说明仍无法确认的部分。"
        if ctx.scope.position:
            prompt += (
                "\n用户当前位置（pos）：「" + ctx.scope.position + "」。"
                "回答位置、就近推荐、导航类问题时以此为参考。"
            )
        # 功能令牌的专属边界追加在通用约束之后：只能收窄能力，不能覆盖安全规则。
        client = getattr(ctx, "client", None)
        if client is not None and getattr(client, "prompt_suffix", ""):
            prompt += "\n" + client.prompt_suffix
        if ctx.preference_unavailable:
            tools = [tool for tool in tools if tool.name != MEMORY_TOOL_NAME]
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
                        "tools": [{"name": c["name"]} for c in message.tool_calls],
                    }
                )
                logger.info(
                    "agent model round",
                    extra={
                        "run_id": ctx.record.run_id,
                        "event_type": "agent_model_round",
                        "fields": {
                            "call": ctx.llm_budget.calls,
                            "tools": [{"name": c["name"]} for c in message.tool_calls],
                        },
                    },
                )
        return response


class SingleAgentRuntime:
    def __init__(self, model: Any, *, checkpointer=None, store=None, memory_service=None):
        self.model = model
        self._checkpointer = checkpointer
        self._store = store
        self.memory_service = memory_service
        self.graph = self._build_agent(
            build_tools(), "按服务端选定的角色方案作答。", "meta_agent_single"
        )

    def _build_agent(self, tools, system_prompt: str, name: str, *, include_memory=True):
        if self.memory_service is not None and include_memory:
            tools = [*tools, build_memory_tool()]
        return create_agent(
            model=self.model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[RoleContextMiddleware(self.memory_service)],
            # Context is runtime-only; it contains authenticated identities and dependencies.
            context_schema=RunContext,
            checkpointer=self._checkpointer,
            store=self._store,
            name=name,
        )

    def sub_agent(self, *, tool_names: frozenset[str], system_prompt: str, name: str):
        """构建仅暴露指定工具子集的子代理（如基础问答 + 指令）。"""
        return self._build_agent(
            build_tools(tool_names),
            system_prompt,
            name,
            include_memory=MEMORY_TOOL_NAME in tool_names,
        )

    async def execute(self, ctx, *, emit_answer: bool = True) -> str | None:
        """运行主单 Agent 循环（全工具集 + 内置系统提示词），返回最终答复文本。"""
        return await self._run(
            ctx,
            self.graph,
            goal_id="agent",
            emit_answer=emit_answer,
            enrich_profile=True,
            system_prompt=None,
            scene_fallback=True,
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
            scene_fallback=False,
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
        scene_fallback: bool = False,
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
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "agent_run_start",
                "fields": {
                    "client": getattr(getattr(ctx, "scope", None), "client_id", "primary")
                },
            },
        )
        try:
            previous_prompt = ctx.system_prompt
            ctx.system_prompt = system_prompt
            try:
                messages = await fresh_history(messages_from_dict(ctx.memory.agent_messages), ctx)
                if enrich_profile and ctx.scope.has_patient:
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
            answer = model_text(answer)
            if scene_fallback:
                answer = await self._deliver_scene_action(ctx, answer)
            processor = ctx.answer_post_processor
            if processor is not None:
                processed = processor(answer, ctx)
                if inspect.isawaitable(processed):
                    processed = await processed
                if not isinstance(processed, str) or not processed.strip():
                    raise DomainError("agent_no_answer", "回答后处理结果为空，请检查后处理逻辑。")
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
                        "client": getattr(getattr(ctx, "scope", None), "client_id", "primary"),
                    },
                },
            )
            return answer
        finally:
            CURRENT_RUN.reset(token)

    # 模型没执行动作时常见的道歉话术，用于判断答复是否可被导航结果替换。
    _SCENE_APOLOGY = re.compile(
        "没认出来|没听懂|没识别|没带您|没给您带|无法带您|无法为您|抱歉|对不起|没找到|查不到"
    )

    async def _deliver_scene_action(self, ctx, answer: str) -> str:
        """模型漏调场景工具时，按原话确定性补执行一次。

        场景指令由 agent 本地解析（scene_local，直连点位库），口语话术归一、
        同义词、拼音兜底与多动作拆句都在本地完成，不再受 AI_WebApi 词表限制。
        但多轮上下文里模型仍可能整轮不调工具、只回道歉话术；此处不依赖模型
        决策，把原话整句交给场景工具，成功下发后用确定性话术替换道歉。
        """
        if ctx.record.action_delivery or not classify_business_query(ctx.query).scene:
            return answer
        if not tool_allowed(ctx, "navigate_scene"):
            return answer
        try:
            await invoke_tool(ctx, "navigate_scene", {"target": ctx.query})
        except DomainError as exc:
            logger.info(
                "scene fallback skipped",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "scene_fallback_skipped",
                    "fields": {"code": exc.code},
                },
            )
            return answer
        if not ctx.record.action_delivery:
            return answer
        names = [
            action.get("matchName")
            for result in ctx.record.results.values()
            for action in (result.outputs.get("actions") or {}).values()
            if action.get("status") == "matched" and action.get("matchName")
        ]
        logger.info(
            "scene fallback delivered",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "scene_fallback_delivered",
                "fields": {"actions": len(ctx.record.action_delivery), "names": names[:3]},
            },
        )
        notice = f"好的，这就带您去{names[0]}。" if names else "好的，已经为您带路。"
        if self._SCENE_APOLOGY.search(answer):
            return notice
        return answer if notice in answer else f"{answer}\n{notice}"
