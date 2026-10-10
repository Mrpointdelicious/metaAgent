"""
创建日期：2026-10-05
文件功能：验证真实create_agent循环、三层患者工具、失败恢复和会话隔离。

该测试使用的是
tool_call("get_patient_consultation"),
tool_call("get_patient_rehab", call_id="call-2"),
AIMessage("您有已记录的膝关节疼痛...")
预设的toolcall，主要是测试工程上的稳健性，还没涉及LLM的表现。
"""

import asyncio
import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from meta_agent.agent.context import compact_result
from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.agent.tools import build_tools
from meta_agent.app import create_app
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import TrustedScope
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


SCOPE = TrustedScope("tenant", "actor", project_patient_id="3799", iretour_patient_id="3799")


# 测试三层工具能否正确调用，名称填写是否正确
# assert语法不熟悉
# 要求表达式为 True，否则抛出 AssertionError，pytest 就会将对应测试判为失败。
def test_three_layers_are_agent_tools_without_model_visible_identity():
    names = {tool.name: tool for tool in build_tools()}
    for name in ["get_patient_profile", "get_patient_consultation", "get_patient_rehab"]:
        assert name in names
    assert "get_multisource_patient_context" not in names
    for tool in names.values():
        assert not {"patientId", "patient_id", "system_context", "robot_patient_id"} & set(
            tool.args
        )


# 测试Agent调用工具的返回测试用函数
# async 是 Python 中用于定义异步函数（协程函数）的关键字，配合 await 使用，实现异步编程。


def test_agent_calls_clinical_and_rehab_tools_and_writes_natural_answer():
    async def check():
        async with runtime() as (c, backend):
            activate(
                c,
                [
                    tool_call("get_patient_consultation"),
                    tool_call("get_patient_rehab", call_id="call-2"),
                    AIMessage(
                        "您有已记录的膝关节疼痛，训练记录中有平衡训练。建议先按既往医嘱执行。"
                    ),
                ],
            )
            run = await c.application.execute(request("结合病史和训练史给我建议", scope=SCOPE))
            assert run.record.status == "succeeded"
            assert "既往医嘱" in answers(run.record)
            assert run.record.plan is None and run.record.decision is None
            assert [name for name, _ in backend.calls] == [
                "get_patient_profile",
                "get_patient_consultation",
                "get_patient_rehab",
            ]
            assert all(p["system_context"]["user"] == "3799" for _, p in backend.calls)
            assert run.record.metrics["llm_calls"] == 3

    asyncio.run(check())


# 测试部分数据无法获取时，其它源数据仍可以获取的例子


def test_tool_failure_returns_to_model_and_other_layer_can_still_answer():
    async def check():
        async with runtime() as (c, backend):
            backend.overrides["get_patient_consultation"] = DomainError("http_503", "就诊查询失败")
            activate(
                c,
                [
                    tool_call("get_patient_consultation"),
                    tool_call("get_patient_rehab", call_id="call-2"),
                    AIMessage("就诊记录暂时无法读取；目前可以确认有平衡训练记录。"),
                ],
            )
            run = await c.application.execute(request("了解我的病史和康复记录", scope=SCOPE))
            assert run.record.status == "partial"
            assert "平衡训练" in answers(run.record)
            assert "get_patient_rehab" in [n for n, _ in backend.calls]
            assert any(r.code == "http_503" for r in run.record.results.values())

    asyncio.run(check())


# 工具调用重试测试
def test_agent_can_retry_tool_after_error_without_recompiling_plan():
    async def check():
        async with runtime() as (c, backend):
            attempt = 0

            def response(payload):
                nonlocal attempt
                attempt += 1
                if attempt == 1:
                    raise DomainError("temporary_failure", "暂时失败", retryable=True)
                from meta_agent.tools.demo import demo_response

                return demo_response("get_patient_consultation", payload)

            backend.overrides["get_patient_consultation"] = response
            activate(
                c,
                [
                    tool_call("get_patient_consultation"),
                    tool_call("get_patient_consultation", {"page_size": 1}, "call-2"),
                    AIMessage("已读取到已记录的膝关节疼痛和医嘱。"),
                ],
            )
            run = await c.application.execute(request("我的病史是什么", scope=SCOPE))
            assert run.record.status == "succeeded"
            assert attempt == 2
            assert any(r.code == "temporary_failure" for r in run.record.results.values())

    asyncio.run(check())


# 测Agent是否记得历史信息
def test_agent_history_keeps_tool_refs_for_followup_and_is_patient_scoped():
    async def check():
        async with runtime() as (c, backend):
            activate(c, [tool_call("get_iretour_session_analysis"), AIMessage("这次是平衡训练。")])
            first = await c.application.execute(request("这次训练是什么", scope=SCOPE))
            memory = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert any("synthetic-tour-1" in json.dumps(m) for m in memory.agent_messages)
            other = TrustedScope(
                "tenant", "actor", project_patient_id="3780", iretour_patient_id="3780"
            )
            assert not (await c.repository.conversation(other.thread_id("c1"))).agent_messages
            activate(c, [AIMessage("刚才这次是平衡训练。")])
            second = await c.application.execute(request("刚才是什么训练", "r2", scope=SCOPE))
            checkpoint = await c.repository.checkpointer.aget_tuple(
                {"configurable": {"thread_id": second.record.run_id}}
            )
            messages = checkpoint.checkpoint["channel_values"]["messages"]
            assert any(m.type == "tool" and "synthetic-tour-1" in str(m.content) for m in messages)
            assert first.record.status == second.record.status == "succeeded"

    asyncio.run(check())


# 测试非患者信息能否会被拒绝进入上下文


def test_patient_result_from_other_identity_is_rejected_and_never_enters_context():
    async def check():
        async with runtime() as (c, backend):
            from meta_agent.tools.demo import demo_response

            bad = demo_response("get_patient_consultation", {"system_context": {"user": "999"}})
            backend.overrides["get_patient_consultation"] = bad
            activate(
                c,
                [tool_call("get_patient_consultation"), AIMessage("这次没有取得可信的就诊记录。")],
            )
            run = await c.application.execute(request("查病史", scope=SCOPE))
            assert any(r.code == "patient_scope_mismatch" for r in run.record.results.values())
            memory = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert "project:999" not in json.dumps(memory.agent_messages)

    asyncio.run(check())


# 测试压缩和截断效果


def test_compaction_keeps_explicit_truncation_and_prescription_name():
    body = {
        "status": "success",
        "data": {"prescriptions": [{"name": "已记录的处方名称", "content": "长文本" * 6000}]},
    }
    # 这里负责压缩
    compact = compact_result(body, 512)
    assert compact["context_truncated"] is True  # 正确压缩
    assert (
        compact["data"]["prescriptions"][0]["name"] == "已记录的处方名称"
    )  # 错误地压缩兜底？截断后诊断名称仍要留存。


# 测试？robot mapping是什么意思？
# robot数据库已经废弃，需要完全使用project


@pytest.mark.parametrize("project_key", ["projectPatientId", "project_patient_id"])
def test_project_native_api_does_not_require_robot_mapping_and_reuses_request(
    monkeypatch, project_key
):
    async def check():
        monkeypatch.setattr(
            "meta_agent.infrastructure.container.create_planner_model",
            lambda _: ScriptedModel(
                messages=iter([tool_call("get_patient_consultation"), AIMessage("已读取病历。")])
            ),
        )
        async with runtime(orchestration_mode="agent", service_bearer_token="test") as (c, backend):
            app = create_app(c.settings)
            app.state.container = c
            payload = {
                "query": "查我的病历",
                "request_id": "native-agent",
                "user": "actor",
                "inputs": {project_key: "3799"},
                "response_mode": "blocking",
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client:
                response = await client.post("/v1/chat", json=payload)
                body = response.json()
                assert response.status_code == 200 and body["status"] == "succeeded"
                assert body["metrics"]["orchestration_mode"] == "agent"
                assert "resolve_patient_identity" not in [name for name, _ in backend.calls]
                count = len(backend.calls)
                again = (await client.post("/v1/chat", json=payload)).json()
                assert again["reused"] and again["run_id"] == body["run_id"]
                assert len(backend.calls) == count
                events = body["events"]
                assert events[0]["type"] == "accepted" and events[-1]["type"] == "completed"
                assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
                access = {"user": "actor", "inputs": {"projectPatientId": "3799"}}
                path = f"/v1/runs/{body['run_id']}/status"
                assert (await client.post(path, json=access)).status_code == 200
                access["inputs"]["projectPatientId"] = "3780"
                assert (await client.post(path, json=access)).status_code == 404
                mixed = {
                    **payload,
                    "request_id": "mixed",
                    "inputs": {project_key: "3799", "patientId": "999"},
                }
                assert (await client.post("/v1/chat", json=mixed)).status_code == 422
                assert len(backend.calls) == count

    asyncio.run(check())


# fix 主动取消正在执行的工具，并检查取消传播、最终状态和没有生成回答。


def test_agent_cancellation_stops_inflight_tool_and_emits_completed():
    async def check():
        async with runtime() as (c, backend):
            activate(c, [tool_call("get_patient_consultation"), AIMessage("不应生成的回答。")])
            backend.gates["get_patient_consultation"] = asyncio.Event()
            req = request("查病史", scope=SCOPE)
            run = await c.application.start(req)
            while "get_patient_consultation" not in backend.entered:
                await asyncio.sleep(0)
            await backend.entered["get_patient_consultation"].wait()
            await c.application.cancel(SCOPE.scope_hash, run.record.run_id)
            # 模拟调用失败返回取消信息
            assert run.record.status == "cancelled"
            assert run.record.events[-1].type == "completed"
            assert "get_patient_consultation" in backend.cancelled
            assert not answers(run.record)

    asyncio.run(check())


# 不同的工具域是否能正常运行
# 接下来要看看后续是如何实现的，搞懂这边价值比较大
# 被配置或身份限制禁用的工具，能否被执行层阻止


def test_agent_cannot_execute_tools_hidden_by_settings_or_identity():
    async def check():
        async with runtime(doctors_enabled=False) as (c, backend):
            activate(c, [tool_call("search_doctors"), AIMessage("医生查询未启用。")])
            run = await c.application.execute(request("查询医生", scope=SCOPE))
            assert "search_doctors" not in [name for name, _ in backend.calls]
            assert any(r.code == "tool_not_allowed" for r in run.record.results.values())

    asyncio.run(check())


# 废弃证据信息更新之前的表现测试
# fix:旧 Evidence 不可用后，历史工具消息被标记为 expired，并要求重新查询


def test_expired_tool_evidence_is_replaced_before_next_model_call():
    async def check():
        async with runtime() as (c, backend):
            activate(c, [tool_call("get_patient_consultation"), AIMessage("已读取病历。")])
            first = await c.application.execute(request("查病史", scope=SCOPE))
            task = next(
                r
                for r in first.record.results.values()
                if r.outputs.get("tool_name") == "get_patient_consultation"
            )
            await c.repository.delete("evidence", SCOPE.scope_hash, task.evidence_ids[0])
            activate(
                c,
                [
                    tool_call("get_patient_consultation", call_id="call-new"),
                    AIMessage("重新查询后已取得病历。"),
                ],
            )
            second = await c.application.execute(request("刚才的医嘱是什么", "r2", scope=SCOPE))
            checkpoint = await c.repository.checkpointer.aget_tuple(
                {"configurable": {"thread_id": second.record.run_id}}
            )
            messages = checkpoint.checkpoint["channel_values"]["messages"]
            old = next(m for m in messages if m.type == "tool" and m.tool_call_id == "call-1")
            assert json.loads(old.content)["status"] == "expired"
            assert "膝关节" not in old.content
            assert [n for n, _ in backend.calls].count("get_patient_consultation") == 2
            assert second.record.status == "succeeded"

    asyncio.run(check())


# 单代理可以完成最后预算模型调用，测试最大调用次数约束下能否正常运作
def test_single_agent_can_finish_at_last_budgeted_model_call(monkeypatch):
    async def check():
        monkeypatch.setattr(
            "meta_agent.infrastructure.container.create_planner_model",
            lambda _: ScriptedModel(
                messages=iter(
                    [tool_call("get_patient_consultation"), AIMessage("根据已读取的病历回答。")]
                )
            ),
        )
        async with runtime(orchestration_mode="agent", agent_max_model_calls=2) as (c, _):
            run = await c.application.execute(request("查病史", scope=SCOPE))
            assert run.record.metrics["llm_calls"] == 2
            assert run.record.status == "succeeded"
            assert "病历" in answers(run.record)

    asyncio.run(check())
