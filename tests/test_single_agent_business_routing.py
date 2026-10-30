"""
创建日期：2026-10-30
文件功能：验证知识和场景路由只使用原有单Agent循环及权限过滤。
"""

import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import Field

from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.agent.tools import available_tools, build_tools, invoke_tool
from meta_agent.domains.routing import classify_business_query
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.integration import context
from tests.helpers.runtime import request, runtime
from tests.test_remote_knowledge import mock_http, response
from tests.test_scene_instruction_integration import scene_body
from tests.test_single_agent import ScriptedModel, tool_call


class RecordingModel(ScriptedModel):
    visible_tools: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.visible_tools.append([tool.name for tool in tools])
        return self


@pytest.mark.parametrize(
    "query,knowledge,scene,patient,history",
    [
        ("介绍左上角", True, False, False, False),
        ("个人中心是什么", True, False, False, False),
        ("这个按钮怎么用", True, False, False, False),
        ("如何打开个人中心", True, False, False, False),
        ("二楼怎么走", True, False, False, False),
        ("去二楼", False, True, False, False),
        ("打开个人中心", False, True, False, False),
        ("切换视角", False, True, False, False),
        ("回到入口", False, True, False, False),
        ("打开我的报告", False, False, True, False),
        ("查训练报告", False, False, True, False),
        ("打开历史对话", False, False, False, True),
        ("去二楼然后查训练报告", False, True, True, False),
        ("打开个人中心然后介绍训练页面", True, True, False, False),
        ("不要打开个人中心", False, False, False, False),
        ("如果我打开个人中心", False, False, False, False),
        ("打开个人中心功能", False, True, False, False),
        ("去二楼并介绍训练页面", True, True, False, False),
        ("能打开个人中心吗", False, False, False, False),
    ],
)
def test_business_intent_distinguishes_questions_commands_and_mixed_requests(
    query,
    knowledge,
    scene,
    patient,
    history,
):
    route = classify_business_query(query)
    assert (route.knowledge, route.scene, route.patient, route.history) == (
        knowledge,
        scene,
        patient,
        history,
    )


def test_route_filter_cannot_grant_patient_operator_or_scene_permissions():
    async def check():
        async with runtime(scene_actions_enabled=False) as (container, backend):
            ctx = context(container, "去二楼然后查训练报告")
            selected = {tool.name for tool in available_tools(ctx, build_tools())}
            assert "get_patient_profile" not in selected
            assert "query_hospital_operations" not in selected
            assert "navigate_scene" not in selected
            result = await invoke_tool(ctx, "navigate_scene", {"target": "去二楼"})
            assert result["code"] == "tool_not_allowed" and not backend.calls
            ctx.query = "个人中心是什么"
            selected = {tool.name for tool in available_tools(ctx, build_tools())}
            assert "resolve_scene_instruction" not in selected and "search_knowledge" in selected
            result = await invoke_tool(ctx, "navigate_scene", {"target": "个人中心"})
            assert result["code"] == "tool_not_allowed" and not backend.calls

    asyncio.run(check())


@pytest.mark.parametrize("mixed", [False, True])
def test_real_single_agent_loop_routes_knowledge_and_mixed_commands_without_extra_calls(
    monkeypatch, mixed
):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=response()))

    async def check():
        sources = [
            {
                "id": "hospital",
                "description": "医院资料",
                "domains": ["help"],
                "scoped_domains": ["help"],
                "url": "https://hospital.test/search",
                "token": "synthetic",
            }
        ]
        async with runtime(knowledge_sources=sources, scene_actions_enabled=True) as (
            container,
            backend,
        ):
            messages = []
            query = "个人中心是什么"
            if mixed:
                query = "打开个人中心然后介绍训练页面"
                backend.overrides["navigate_scene"] = scene_body(["打开个人中心"])
                messages.append(
                    tool_call("navigate_scene", {"target": "打开个人中心"}, call_id="scene")
                )
            messages.extend(
                [
                    tool_call(
                        "search_knowledge",
                        {"query": "个人中心说明", "domain": "help"},
                        call_id="knowledge",
                    ),
                    AIMessage("根据医院资料说明个人中心；动作交付结果尚待确认。"),
                ]
            )
            model = RecordingModel(messages=iter(messages))
            agent = SingleAgentRuntime(model, checkpointer=container.repository.checkpointer)
            assert not hasattr(agent, "sub_agent") and not hasattr(agent, "execute_sub")
            container.application.agent_runtime = agent
            scope = TrustedScope("tenant", "actor", space_id="space", scene_version=7)
            run = await container.application.execute(request(query, scope=scope))
            assert run.record.status == "succeeded"
            assert run.record.metrics["llm_calls"] == 2 + int(mixed)
            assert run.record.metrics["tool_calls"] == 1 + int(mixed)
            assert all(("navigate_scene" in tools) == mixed for tools in model.visible_tools)
            assert bool(run.record.action_delivery) == mixed
            assert run.record.plan is None

    asyncio.run(check())
