"""
创建日期：2026-11-17
文件功能：验证 LangMem 偏好读写、身份隔离、动态上下文及历史证据保留。
"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import Field, ValidationError

from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.api.security import ClientIdentity
from meta_agent.application.conversations import owner_key
from meta_agent.memory.context import project_preference_history
from meta_agent.memory.repository import InMemoryPreferenceRepository
from meta_agent.memory.schemas import TOOL_NAME, MemoryArgs, PreferenceError
from meta_agent.memory.service import PreferenceToolStore, build_memory_tool
from tests.helpers.identity import patient_scope
from tests.helpers.runtime import answers, request, runtime
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import ScriptedModel, tool_call

SCOPE = patient_scope("tenant", "actor", "3799")


def preference(value="short", key="answer_length", **extra):
    return {"content": {"key": key, "value": value}, **extra}


class RecordingModel(ScriptedModel):
    seen: list = Field(default_factory=list)
    seen_tools: list = Field(default_factory=list)

    def bind_tools(self, tools, *args, **kwargs):
        self.seen_tools.append([t.name for t in tools])
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(deepcopy(messages))
        return super()._generate(messages, *args, **kwargs)


def activate(c, messages):
    model = RecordingModel(messages=iter(messages))
    c.application.agent_runtime = SingleAgentRuntime(
        model,
        checkpointer=c.repository.checkpointer,
        store=c.repository.store,
        memory_service=c.patient_memory,
    )
    return model


def memory_results(messages):
    return [
        json.loads(m.content)
        for m in messages
        if isinstance(m, ToolMessage) and m.name == TOOL_NAME
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"content": {"key": "diagnosis", "value": "糖尿病"}},
        {"content": {"key": "answer_length", "value": "糖尿病"}},
        {"content": {"key": "answer_length", "value": "short", "reason": "糖尿病"}},
        {"content": {"key": "language", "value": "short"}},
        {**preference(), "reason": "糖尿病"},
        {**preference(), "owner": "other"},
        {**preference(), "id": str(uuid4())},
        preference(action="update"),
        {"content": None},
        {"action": "delete", "id": str(uuid4())},
    ],
)
def test_closed_memory_schema_rejects_medical_fields_and_invalid_operations(args):
    with pytest.raises(ValidationError):
        MemoryArgs.model_validate(args)


def test_native_langmem_tool_writes_only_to_bound_preference_store():
    async def check():
        repo = InMemoryPreferenceRepository()
        owner = owner_key(SCOPE)
        args = MemoryArgs.model_validate(preference())
        store = PreferenceToolStore(repo, owner, args)
        tool = build_memory_tool(store, owner)
        assert tool.args["action"]["enum"] == ["create", "update"]
        assert not {"owner", "patient_id", "namespace"}.intersection(tool.args)
        await tool.ainvoke(preference())
        assert (await repo.snapshot(owner)).items[0].content.value == "short"
        memory_id = store.change.id
        updated = MemoryArgs.model_validate(preference("detailed", action="update", id=memory_id))
        await build_memory_tool(PreferenceToolStore(repo, owner, updated), owner).ainvoke(
            updated.model_dump(mode="json")
        )
        assert (await repo.snapshot(owner)).items[0].content.value == "detailed"
        with pytest.raises(PreferenceError, match="not_authorized"):
            await build_memory_tool().ainvoke(preference())

    asyncio.run(check())


def test_agent_updates_context_and_preserves_medical_history_evidence_and_checkpoint():
    async def check():
        async with runtime(patient_memory_enabled=True) as (c, backend):
            model = activate(
                c,
                [
                    tool_call(TOOL_NAME, preference("detailed")),
                    tool_call("get_patient_consultation", call_id="clinical"),
                    AIMessage("我会详细说明，并依据已查询的就诊记录回答。"),
                ],
            )
            run = await c.application.execute(
                request("我有糖尿病，以后请详细回答，再查询病史", scope=SCOPE)
            )
            assert run.record.status == "succeeded"
            items = (await c.patient_memory.repository.snapshot(owner_key(SCOPE))).items
            assert len(items) == 1 and items[0].content.value == "detailed"
            assert "糖尿病" not in json.dumps(
                [i.content.model_dump() for i in items], ensure_ascii=False
            )
            assert "detailed" in model.seen[1][0].content
            assert "3句话以内" not in model.seen[1][0].content
            assert "仅是交流偏好，不是医疗事实" in model.seen[1][0].content
            result = memory_results(model.seen[1])[0]
            assert result["memory_id"] == str(items[0].id)
            assert result["status"] == "success"
            history = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert "糖尿病" in json.dumps(history.agent_messages, ensure_ascii=False)
            assert "<patient_response_preferences>" not in json.dumps(history.agent_messages)
            clinical = next(
                r
                for r in run.record.results.values()
                if r.outputs.get("tool_name") == "get_patient_consultation"
            )
            assert await c.repository.evidence(SCOPE.scope_hash, clinical.evidence_ids[0])
            checkpoint = await c.repository.checkpointer.aget_tuple(
                {"configurable": {"thread_id": run.record.run_id}}
            )
            assert "<patient_response_preferences>" not in str(checkpoint.checkpoint)
            assert c.patient_memory.repository is not c.repository.store
            assert "get_patient_consultation" in [name for name, _ in backend.calls]

    asyncio.run(check())


def test_shared_account_preferences_across_conversations_and_query_scopes():
    async def check():
        async with runtime(patient_memory_enabled=True) as (c, _):
            activate(c, [tool_call(TOOL_NAME, preference()), AIMessage("已记住。")])
            await c.application.execute(request("以后简短回答", scope=SCOPE))
            other_scope = patient_scope(
                "tenant", "actor", "3799", data_scope="current_tenant", current_business_id=7
            )
            assert other_scope.scope_hash != SCOPE.scope_hash
            model = activate(c, [AIMessage("好的。")])
            await c.application.execute(
                request("你好", "r2", scope=other_scope, conversation="another")
            )
            assert '"value":"short"' in model.seen[0][0].content
            for scope in [
                replace(SCOPE, end_user_id="another"),
                replace(SCOPE, tenant_id="another"),
                replace(SCOPE, client_id="secondary"),
            ]:
                assert not (await c.patient_memory.repository.snapshot(owner_key(scope))).items

    asyncio.run(check())


def test_repeated_create_upserts_and_update_requires_owned_id_and_matching_key():
    async def check():
        repo = InMemoryPreferenceRepository()
        owner = owner_key(SCOPE)
        first = await repo.apply(owner, MemoryArgs.model_validate(preference()), uuid4())
        changes = await asyncio.gather(
            *[repo.apply(owner, MemoryArgs.model_validate(preference()), uuid4()) for _ in range(6)]
        )
        assert all(change.id == first.id for change in changes)
        update = MemoryArgs.model_validate(preference("detailed", action="update", id=first.id))
        await repo.apply(owner, update, uuid4())
        assert len((await repo.snapshot(owner)).items) == 1
        with pytest.raises(PreferenceError, match="not_found"):
            await repo.apply(owner_key(replace(SCOPE, end_user_id="another")), update, uuid4())
        with pytest.raises(PreferenceError, match="key_mismatch"):
            await repo.apply(
                owner,
                MemoryArgs.model_validate(
                    preference("en", "language", action="update", id=first.id)
                ),
                uuid4(),
            )

    asyncio.run(check())


def test_invalid_model_arguments_return_safe_errors_and_no_memory():
    async def check():
        async with runtime(patient_memory_enabled=True) as (c, _):
            model = activate(
                c,
                [
                    tool_call(TOOL_NAME, {**preference(), "reason": "糖尿病"}),
                    AIMessage("医疗信息不会保存为长期偏好。"),
                ],
            )
            await c.application.execute(request("记住我的糖尿病", scope=SCOPE))
            result = memory_results(model.seen[1])[0]
            assert result == {"status": "error", "code": "invalid_memory_arguments"}
            assert not (await c.patient_memory.repository.snapshot(owner_key(SCOPE))).items

    asyncio.run(check())


@pytest.mark.parametrize("gate", ["disabled", "clinician", "unbound", "client", "budget"])
def test_hidden_or_disallowed_memory_calls_cannot_write(gate):
    async def check():
        async with runtime(
            patient_memory_enabled=gate != "disabled", max_tool_calls=1 if gate == "budget" else 16
        ) as (c, _):
            scope = SCOPE
            if gate == "clinician":
                scope = replace(scope, role="clinician")
            if gate == "unbound":
                scope = replace(scope, patient_ref=None)
            model = activate(c, [tool_call(TOOL_NAME, preference()), AIMessage("继续回答。")])
            req = request("以后简短回答", scope=scope)
            if gate == "client":
                req = replace(req, client=ClientIdentity("primary", frozenset({TOOL_NAME})))
            await c.application.execute(req)
            assert all(TOOL_NAME not in names for names in model.seen_tools)
            if c.patient_memory:
                assert not (await c.patient_memory.repository.snapshot(owner_key(scope))).items

    asyncio.run(check())


def test_memory_read_failure_falls_back_without_blocking_answer(monkeypatch):
    async def broken(*args):
        raise RuntimeError("sensitive backend detail")

    async def check():
        async with runtime(patient_memory_enabled=True) as (c, _):
            monkeypatch.setattr(c.patient_memory.repository, "snapshot", broken)
            model = activate(c, [AIMessage("您好。")])
            run = await c.application.execute(request("你好", scope=SCOPE))
            assert "您好" in answers(run.record)
            assert "记忆暂时不可用" in model.seen[0][0].content
            assert "sensitive backend detail" not in str(model.seen)
            assert TOOL_NAME not in model.seen_tools[0]

    asyncio.run(check())


def test_history_projection_preserves_tool_pairs_and_does_not_mutate_archive():
    messages = [
        HumanMessage("以前说过的偏好"),
        AIMessage(
            "",
            tool_calls=[
                {"name": TOOL_NAME, "args": preference(), "id": "memory"},
                {"name": "get_patient_consultation", "args": {}, "id": "medical"},
            ],
        ),
        ToolMessage("旧偏好", tool_call_id="memory", name=TOOL_NAME),
        ToolMessage("保留医疗证据", tool_call_id="medical"),
        HumanMessage("本轮问题"),
    ]
    before = deepcopy(messages)
    projected = project_preference_history(messages)
    assert messages == before
    assert projected[1].tool_calls == [messages[1].tool_calls[1]]
    assert projected[2].content == "保留医疗证据"
    assert len(projected) == 4


def test_each_model_context_read_uses_latest_store_values():
    async def check():
        async with runtime(patient_memory_enabled=True) as (c, _):
            ctx = SimpleNamespace(
                scope=SCOPE,
                settings=c.settings,
                remaining=30,
                preference_owner=None,
                preference_unavailable=False,
            )
            service = c.patient_memory
            await service.repository.apply(
                owner_key(SCOPE), MemoryArgs.model_validate(preference()), uuid4()
            )
            assert '"value":"short"' in await service.model_context(ctx)
            await service.repository.apply(
                owner_key(SCOPE), MemoryArgs.model_validate(preference("detailed")), uuid4()
            )
            text = await service.model_context(ctx)
            assert '"value":"detailed"' in text and '"value":"short"' not in text

    asyncio.run(check())


def test_memory_write_failure_returns_safe_error_and_answer_continues(monkeypatch):
    async def broken(*args):
        raise RuntimeError("private connection detail")

    async def check():
        async with runtime(patient_memory_enabled=True) as (c, _):
            monkeypatch.setattr(c.patient_memory.repository, "apply", broken)
            model = activate(
                c, [tool_call(TOOL_NAME, preference()), AIMessage("暂时无法保存偏好。")]
            )
            run = await c.application.execute(request("以后简短回答", scope=SCOPE))
            assert "暂时无法保存" in answers(run.record)
            assert memory_results(model.seen[1])[0]["code"] == "memory_unavailable"
            assert "private connection detail" not in str(model.seen)

    asyncio.run(check())


def test_read_timeout_is_bounded_and_disables_writes_for_this_request():
    async def check():
        from meta_agent.memory.service import PatientMemoryService

        async def slow(*args):
            await asyncio.Event().wait()

        service = PatientMemoryService(SimpleNamespace(snapshot=slow))
        ctx = SimpleNamespace(
            scope=SCOPE,
            settings=AppTestSettings(
                patient_memory_enabled=True, patient_memory_timeout_seconds=0.1
            ),
            remaining=30,
            preference_owner=None,
            preference_unavailable=False,
        )
        text = await asyncio.wait_for(service.model_context(ctx), timeout=1)
        assert "记忆暂时不可用" in text and ctx.preference_unavailable

    asyncio.run(check())


def test_container_wires_memory_service_into_agent(monkeypatch):
    async def check():
        model = RecordingModel(
            messages=iter(
                [
                    tool_call(TOOL_NAME, preference("en", "language")),
                    AIMessage("I'll use English."),
                ]
            )
        )
        monkeypatch.setattr(
            "meta_agent.infrastructure.container.create_planner_model", lambda _: model
        )
        async with runtime(patient_memory_enabled=True, orchestration_mode="agent") as (c, _):
            run = await c.application.execute(request("以后请用英文回答", scope=SCOPE))
            assert run.record.status == "succeeded"
            assert "English" in answers(run.record)
            snapshot = await c.patient_memory.repository.snapshot(owner_key(SCOPE))
            assert snapshot.items[0].content.value == "en"
            assert '"value":"en"' in model.seen[1][0].content

    asyncio.run(check())


def test_patient_basic_command_subagent_can_use_same_preference_service():
    async def check():
        from meta_agent.application.service import ApplicationRequest

        async with runtime(patient_memory_enabled=True) as (c, _):
            activate(
                c,
                [
                    AIMessage('{"category":"基础问答","reason":"合成偏好测试"}'),
                    tool_call(TOOL_NAME, preference("en", "language")),
                    AIMessage("Okay."),
                ],
            )
            run = await c.application.execute(
                ApplicationRequest(
                    "以后请用英文回答",
                    SCOPE,
                    "",
                    "room-preference",
                    raw_inputs={"pos": "问诊房间"},
                    protocol="dify",
                )
            )
            assert run.record.status == "succeeded"
            items = (await c.patient_memory.repository.snapshot(owner_key(SCOPE))).items
            assert len(items) == 1 and items[0].content.value == "en"

    asyncio.run(check())
