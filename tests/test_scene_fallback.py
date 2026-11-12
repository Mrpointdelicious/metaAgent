"""
创建日期：2026-11-12
文件功能：验证模型漏调场景工具时的确定性兜底（不改上游匹配器）。
"""

import asyncio

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from meta_agent.agent.runtime import SingleAgentRuntime
from tests.helpers.runtime import answers, request, runtime


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, *args, **kwargs):
        return self


def tool_call(name, args=None, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args or {}}])


def activate(container, messages):
    container.application.agent_runtime = SingleAgentRuntime(
        ScriptedModel(messages=iter(messages)),
        checkpointer=container.repository.checkpointer,
        store=container.repository.store,
    )


def scene_body(text, *, status="matched", name="四楼评估室", code="[ScenePoint:]23"):
    return {
        "status": 200,
        "data": {
            "spaceId": "space",
            "version": 7,
            "commands": [
                {
                    "order": 1,
                    "sourceText": text,
                    "status": status,
                    "result": code if status == "matched" else None,
                    "intentType": "scene_point" if status == "matched" else None,
                    "matchName": name if status == "matched" else None,
                }
            ],
        },
    }


APOLOGY = "大爷，这句系统没认出来，没给您带过去，您再试一次。"


def test_missing_tool_call_still_delivers_the_scene_action():
    """模型整轮没调工具、只回道歉时，用原话确定性补执行并替换道歉话术。"""

    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_body("去四楼")
            activate(c, [AIMessage(APOLOGY)])
            run = await c.application.execute(request("带我回四楼"))
            assert run.record.status == "succeeded"
            assert len(run.record.action_delivery) == 1
            assert "四楼评估室" in answers(run.record)
            assert "没认出来" not in answers(run.record)
            assert [name for name, _ in backend.calls if name == "navigate_scene"] == [
                "navigate_scene"
            ]

    asyncio.run(check())


def test_existing_delivery_is_never_repeated():
    """模型已经执行过动作时兜底不介入，避免重复下发。"""

    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_body("去四楼")
            activate(
                c,
                [
                    tool_call("navigate_scene", {"target": "带我去四楼"}),
                    AIMessage("已经为您带路，请跟着屏幕上的提示走。"),
                ],
            )
            run = await c.application.execute(request("带我去四楼"))
            assert len(run.record.action_delivery) == 1
            assert [name for name, _ in backend.calls if name == "navigate_scene"] == [
                "navigate_scene"
            ]
            text = answers(run.record)
            assert "已经为您带路" in text and "这就带您去" not in text

    asyncio.run(check())


def test_non_scene_question_is_untouched():
    """非场景动作意图（介绍/询问）不触发兜底，也不会多打一次场景接口。"""

    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            activate(c, [AIMessage("四楼是评估中心。")])
            run = await c.application.execute(request("介绍一下四楼"))
            assert [name for name, _ in backend.calls if name == "navigate_scene"] == []
            assert "四楼是评估中心" in answers(run.record)
            assert not run.record.action_delivery

    asyncio.run(check())


def test_unmatched_fallback_keeps_the_original_answer():
    """兜底解析未命中时保留原答复，不伪造动作。"""

    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_body("去四楼", status="unmatched")
            activate(c, [AIMessage(APOLOGY)])
            run = await c.application.execute(request("带我回四楼"))
            assert not run.record.action_delivery
            assert APOLOGY in answers(run.record)

    asyncio.run(check())


def test_history_back_gets_deterministic_guidance():
    """「退回上一个场景」AI_WebApi 无对应点位，兜底给出确定性引导，不空道歉、不误动作。"""

    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            activate(c, [AIMessage(APOLOGY)])
            run = await c.application.execute(request("退回上一个场景"))
            text = answers(run.record)
            assert "目前还不支持退回上一个场景" in text
            assert "大爷" not in text
            assert not run.record.action_delivery
            assert [name for name, _ in backend.calls if name == "navigate_scene"] == []

    asyncio.run(check())
