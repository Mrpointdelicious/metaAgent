"""
创建日期：2026-11-16
文件功能：验证多接入方令牌表：认证映射、独立流程分支绑定、工具白名单与各功能流程。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from meta_agent.agent.tools import tool_allowed
from meta_agent.api.security import (
    ClientIdentity,
    client_registry,
    require_service_identity,
)
from meta_agent.domains.routing import route_from_inputs
from meta_agent.flows.consultation_room.subtasks import _summary
from tests.helpers.settings import AppTestSettings

FUNCTION_CLIENTS = [
    {
        "name": "summary",
        "token": "app-Fzo1re5FHGQeL2FW6CTuBDb5",
        "flow": "ai_summary",
        "allowed_tools": ["open_chat_history"],
        "prompt_suffix": "你只负责整理问诊纪要。",
    },
    {
        "name": "medical-order",
        "token": "app-6xryPStTsxxhhQbsC8QDKlRX",
        "flow": "ai_medical_order",
        "allowed_tools": ["get_patient_consultation", "search_knowledge"],
    },
    {
        "name": "prescription",
        "token": "app-LKmChfdO2GmwTxKejF6DdCds",
        "flow": "ai_prescription",
        "allowed_tools": ["get_patient_consultation", "search_knowledge"],
    },
    {
        "name": "realtime-analysis",
        "token": "app-ivn6nzx5loogmlTkyRSTBcIp",
        "flow": "ai_realtime_analysis",
        "allowed_tools": ["get_patient_rehab", "get_irego_patient_history"],
    },
    {
        "name": "consultation",
        "token": "app-pBgnKabSNONfYpT8j4MqRzHN",
        "flow": "ai_consultation",
    },
]


def _settings(**overrides) -> AppTestSettings:
    values = {
        "app_env": "test",
        "service_bearer_token": "test-primary-token",
        "service_bearer_token_secondary": "test-secondary-token",
        "service_clients": FUNCTION_CLIENTS,
    }
    values.update(overrides)
    return AppTestSettings(**values)


def _request(token: str | None, settings) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/chat-messages",
        "headers": headers,
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
        "app": SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(settings=settings))),
    }
    return Request(scope)


def test_registry_merges_legacy_and_function_clients():
    entries = client_registry(_settings())
    assert len(entries) == 7
    assert [identity.name for _, identity in entries] == [
        "primary",
        "secondary",
        "summary",
        "medical-order",
        "prescription",
        "realtime-analysis",
        "consultation",
    ]


@pytest.mark.parametrize(
    ("token", "name", "flow"),
    [
        ("test-primary-token", "primary", ""),
        ("test-secondary-token", "secondary", ""),
        ("app-Fzo1re5FHGQeL2FW6CTuBDb5", "summary", "ai_summary"),
        ("app-6xryPStTsxxhhQbsC8QDKlRX", "medical-order", "ai_medical_order"),
        ("app-LKmChfdO2GmwTxKejF6DdCds", "prescription", "ai_prescription"),
        ("app-ivn6nzx5loogmlTkyRSTBcIp", "realtime-analysis", "ai_realtime_analysis"),
        ("app-pBgnKabSNONfYpT8j4MqRzHN", "consultation", "ai_consultation"),
    ],
)
def test_each_token_maps_to_its_own_profile(token, name, flow):
    identity = asyncio.run(require_service_identity(_request(token, _settings())))
    assert identity.name == name
    assert identity.flow == flow


def test_function_client_carries_tool_whitelist_and_prompt_suffix():
    identity = asyncio.run(
        require_service_identity(_request("app-Fzo1re5FHGQeL2FW6CTuBDb5", _settings()))
    )
    assert identity.allowed_tools == frozenset({"open_chat_history"})
    assert identity.prompt_suffix == "你只负责整理问诊纪要。"


def test_unknown_token_is_rejected_even_when_function_clients_exist():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_service_identity(_request("app-not-configured", _settings())))
    assert exc.value.status_code == 401


def test_missing_token_is_rejected_when_registry_not_empty():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_service_identity(_request(None, _settings())))
    assert exc.value.status_code == 401


def test_duplicate_client_name_is_rejected():
    duplicated = [FUNCTION_CLIENTS[0], {**FUNCTION_CLIENTS[1], "name": "summary"}]
    with pytest.raises(ValidationError):
        _settings(service_clients=duplicated)


def test_duplicate_token_is_rejected():
    duplicated = [
        FUNCTION_CLIENTS[0],
        {**FUNCTION_CLIENTS[1], "token": FUNCTION_CLIENTS[0]["token"]},
    ]
    with pytest.raises(ValidationError):
        _settings(service_clients=duplicated)


def test_client_cannot_reuse_legacy_name_or_token():
    with pytest.raises(ValidationError):
        _settings(
            service_clients=[{**FUNCTION_CLIENTS[0], "name": "primary"}],
        )
    with pytest.raises(ValidationError):
        _settings(
            service_clients=[{**FUNCTION_CLIENTS[0], "token": "test-primary-token"}],
        )


def test_allowed_and_excluded_tools_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        _settings(
            service_clients=[
                {
                    "name": "broken",
                    "token": "app-broken-token",
                    "allowed_tools": ["search_knowledge"],
                    "excluded_tools": ["search_doctors"],
                }
            ]
        )


def test_unknown_flow_is_reported_at_startup():
    settings = _settings(
        service_clients=[{"name": "broken", "token": "app-broken-token", "flow": "unknown"}]
    )
    assert any("flow" in issue for issue in settings.runtime_issues())


def test_forced_flow_overrides_position():
    assert (
        route_from_inputs({"pos": "二楼 问诊中心"}, forced="ai_medical_order").name
        == "ai_medical_order"
    )
    assert route_from_inputs({"pos": "三楼 问诊房间A"}, forced="ai_summary").name == "ai_summary"
    assert route_from_inputs({"pos": "三楼 问诊房间A"}).name == "consultation_room"


def test_all_function_flows_are_registered_as_independent_branches():
    from meta_agent.flows import register_flows

    application = SimpleNamespace(flows={})
    register_flows(application)
    assert set(application.flows) == {
        "default",
        "consultation_room",
        "ai_summary",
        "ai_medical_order",
        "ai_prescription",
        "ai_realtime_analysis",
        "ai_consultation",
    }
    # 每个功能流程是独立 Flow 实例，互不共享。
    assert application.flows["ai_summary"] is not application.flows["ai_medical_order"]


def test_forced_flow_selects_registered_flow():
    from meta_agent.flows import register_flows

    application = SimpleNamespace(flows={})
    register_flows(application)
    for client in FUNCTION_CLIENTS:
        flow = application.flows[client["flow"]]
        assert getattr(flow, "branch", "") == client["flow"]


def test_same_user_keeps_separate_sessions_per_client():
    from meta_agent.orchestration.identity import TrustedScope

    summary = TrustedScope("tenant", "doctor-1", client_id="summary")
    order = TrustedScope("tenant", "doctor-1", client_id="medical-order")
    # client_id 进入 scope_hash，会话、证据和运行记录都按令牌隔离。
    assert summary.scope_hash != order.scope_hash
    assert summary.thread_id("conversation-1") != order.thread_id("conversation-1")


def _tool_ctx(identity: ClientIdentity) -> SimpleNamespace:
    return SimpleNamespace(
        client=identity,
        settings=SimpleNamespace(
            iretour_reports_enabled=False,
            scene_enabled=True,
            scene_actions_enabled=False,
            doctors_enabled=True,
            hospital_ops_open_to_all_roles=True,
            knowledge_backend="maxkb",
        ),
        scope=SimpleNamespace(has_patient=True, role="patient"),
        record=SimpleNamespace(raw_inputs={}),
    )


def test_allowed_tools_whitelist_hides_everything_else():
    identity = ClientIdentity("summary", allowed_tools=frozenset({"open_chat_history"}))
    ctx = _tool_ctx(identity)
    assert tool_allowed(ctx, "open_chat_history") is True
    assert tool_allowed(ctx, "search_knowledge") is False
    assert tool_allowed(ctx, "get_patient_profile") is False


def test_legacy_client_without_whitelist_keeps_all_tools():
    ctx = _tool_ctx(ClientIdentity("primary"))
    assert tool_allowed(ctx, "search_knowledge") is True
    assert tool_allowed(ctx, "get_patient_profile") is True


def _task_flow_ctx(query: str = "帮我处理") -> SimpleNamespace:
    return SimpleNamespace(
        query=query,
        memory=SimpleNamespace(turns=[], previous_category=""),
        record=SimpleNamespace(
            run_id="run_flow",
            metrics={},
            goal_statuses={},
            status="succeeded",
        ),
        llm_budget=_FakeBudget(),
        emitter=None,
        # 功能分支会读业务输入与 dry_run：默认无 inputs、按 dry_run 不出网。
        business_inputs={},
        settings=SimpleNamespace(dry_run=True),
    )


def test_medical_order_flow_runs_own_pipeline_and_goal(monkeypatch):
    """ai_medical_order 是独立分支：整条 execute 走自己的管线，goal 按分支记账。"""
    from meta_agent.flows import ai_medical_order

    calls: list[tuple[str, str]] = []

    async def fake_run_medical_order(ctx, query, model):
        calls.append((query, type(model).__name__))
        return "康复医嘱（草稿·待医生确认）"

    monkeypatch.setattr(ai_medical_order, "run_medical_order", fake_run_medical_order)
    model = _FakeModel("")
    emitter = _FakeEmitter()
    ctx = _task_flow_ctx("帮患者开医嘱")
    ctx.emitter = emitter
    flow = ai_medical_order.Flow(SimpleNamespace(agent_runtime=SimpleNamespace(model=model)))

    asyncio.run(flow.execute(ctx))

    assert calls == [("帮患者开医嘱", "_FakeModel")]
    assert ctx.record.goal_statuses["ai_medical_order:医嘱生成"] == "succeeded"
    assert ctx.record.metrics["medical_order_filled"] is True
    assert emitter.events[0][0] == "answer_part"
    assert "草稿" in emitter.events[0][1]["text"]


def test_summary_flow_builds_transcript_and_uses_own_goal():
    """没有院内记录时退回会话记忆：本轮 query 进 arg1，历史轮次进 result。"""
    from meta_agent.flows.ai_summary import Flow as SummaryFlow

    model = _FakeModel(
        "【部分对话总结】\n[患者]：头痛三天。（起止时间：10:00:00-10:01:00）\n"
        "【全部对话总结】\n患者头痛三天，医生询问有无发热。"
    )
    emitter = _FakeEmitter()
    ctx = _task_flow_ctx("我头痛三天了")
    ctx.memory = SimpleNamespace(turns=[{"user": "我头痛三天了", "assistant": "有无发热？"}])
    ctx.emitter = emitter
    flow = SummaryFlow(SimpleNamespace(agent_runtime=SimpleNamespace(model=model)))

    asyncio.run(flow.execute(ctx))

    assert "患者：我头痛三天了" in model.prompt
    assert "医生：有无发热？" in model.prompt
    assert "【部分对话总结】" in model.prompt
    assert ctx.record.goal_statuses["ai_summary:纪要总结"] == "succeeded"
    assert ctx.record.metrics["summary_source"] == "session"
    assert emitter.events[0][0] == "answer_part"

    summary = json.loads(emitter.events[0][1]["text"])
    assert "头痛三天" in summary["part"]
    assert "有无发热" in summary["all"]


def test_summary_flow_accepts_dify_record_array_query():
    """query 直接给记录数组（role/timeStamp/from/message）时要按对话处理，并用真实时间纠正区间。"""
    import json as _json

    from meta_agent.flows.ai_summary import Flow as SummaryFlow

    raw = _json.dumps(
        [
            _json.dumps(
                {
                    "role": "Doctor",
                    "timeStamp": 1794804254821,
                    "from": "潘x",
                    "message": "我膝盖疼了两周，走路多了更疼。。",
                },
                ensure_ascii=False,
            ),
            _json.dumps(
                {
                    "role": "Doctor",
                    "timeStamp": 1794804267261,
                    "from": "潘x",
                    "message": "先做个膝关节功能评估，暂时别急着用药。。",
                },
                ensure_ascii=False,
            ),
        ],
        ensure_ascii=False,
    )

    model = _FakeModel(
        "【部分对话总结】\n"
        "[潘x]：患者主诉膝关节疼痛持续两周。（起止时间：00:00:00-00:00:12）\n"
        "[患者]：膝关节疼痛两周。（起止时间：00:00:00-00:00:12）\n"
        "【全部对话总结】\n患者膝关节疼痛两周，医生建议先做功能评估。"
    )
    emitter = _FakeEmitter()
    ctx = _task_flow_ctx(raw)
    ctx.emitter = emitter
    flow = SummaryFlow(SimpleNamespace(agent_runtime=SimpleNamespace(model=model)))

    asyncio.run(flow.execute(ctx))

    # 渲染给模型的是「[时间] 姓名（角色）：内容」，不是原始 JSON 串
    assert "[2026-11-16 12:44:14] 潘x（医生）：我膝盖疼了两周" in model.prompt
    assert ctx.record.metrics["summary_source"] == "query"
    assert ctx.record.metrics["summary_time_span"] == "12:44:14-12:44:27"

    summary = _json.loads(emitter.events[0][1]["text"])
    assert "00:00:00" not in summary["part"]
    assert summary["part"].count("（起止时间：12:44:14-12:44:27）") == 2
    assert "功能评估" in summary["all"]


def test_summary_flow_pulls_chat_records_when_inputs_carry_phone(monkeypatch):
    """inputs 带手机号且非 dry_run 时，纪要要用院内真实记录，并把时间换成北京时间。"""
    import meta_agent.flows.ai_summary as summary
    from meta_agent.flows.ai_summary import Flow as SummaryFlow

    fetched: list[str] = []

    async def fake_login(client, settings):
        return "token"

    async def fake_chat_records(client, token, phone):
        fetched.append(phone)
        return json.dumps(
            {
                "code": 200,
                "message": "ok",
                "data": [
                    {
                        "timeStamp": "1700000000",
                        "part": "起止时间：02:10:36-02:12:00 患者说膝盖疼了两周",
                    }
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(summary, "_login", fake_login)
    monkeypatch.setattr(summary, "_get_chat_records", fake_chat_records)

    model = _FakeModel("【部分对话总结】\n[患者]：膝盖疼两周。\n【全部对话总结】\n同上。")
    ctx = _task_flow_ctx("把这次问诊整理成纪要")
    ctx.business_inputs = {"requesterphone": "15932910402", "patientphone": "15932910402"}
    ctx.settings = SimpleNamespace(dry_run=False)
    ctx.emitter = _FakeEmitter()
    flow = SummaryFlow(SimpleNamespace(agent_runtime=SimpleNamespace(model=model)))

    asyncio.run(flow.execute(ctx))

    assert fetched == ["15932910402"]
    assert "膝盖疼了两周" in model.prompt
    # 02:10:36 UTC → 10:10:36 北京时间
    assert "10:10:36-10:12:00" in model.prompt
    assert ctx.record.metrics["summary_source"] == "chat_records"


def test_summary_flow_skips_clinical_call_in_dry_run(monkeypatch):
    """dry_run 下不能因为 inputs 带手机号就去连院内接口。"""

    async def explode(*args, **kwargs):
        raise AssertionError("dry_run 不应访问院内接口")

    import meta_agent.flows.ai_summary as summary
    from meta_agent.flows.ai_summary import Flow as SummaryFlow

    monkeypatch.setattr(summary, "_login", explode)
    model = _FakeModel("【部分对话总结】\n本段无有效医疗对话内容。\n【全部对话总结】\n无。")
    ctx = _task_flow_ctx("总结一下")
    ctx.business_inputs = {"requesterphone": "15932910402"}
    ctx.emitter = _FakeEmitter()
    flow = SummaryFlow(SimpleNamespace(agent_runtime=SimpleNamespace(model=model)))

    asyncio.run(flow.execute(ctx))

    assert "膝盖疼" not in model.prompt
    assert ctx.record.metrics["summary_source"] == "session"


def test_consultation_flow_uses_classifier_with_own_goal(monkeypatch):
    """ai_consultation 复用问诊分类器，但 goal 按自己的分支名记账。"""
    import meta_agent.flows.consultation_room as room
    from meta_agent.flows.ai_consultation import Flow as ConsultationFlow

    routed: list[str] = []

    async def fake_route(ctx, category, reason, application):
        routed.append(category)

    async def fake_ainvoke(self, prompt, config=None):
        return SimpleNamespace(content='{"category": "医嘱填写", "reason": "明确要求"}')

    monkeypatch.setattr(room, "route", fake_route)
    monkeypatch.setattr(_FakeModel, "ainvoke", fake_ainvoke)
    ctx = _task_flow_ctx("帮我写医嘱")
    flow = ConsultationFlow(SimpleNamespace(agent_runtime=SimpleNamespace(model=_FakeModel(""))))

    asyncio.run(flow.execute(ctx))

    assert routed == ["医嘱填写"]
    assert ctx.record.goal_statuses["ai_consultation"] == "succeeded"
    assert ctx.record.metrics["task_category"] == "医嘱填写"


class _FakeModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompt = ""

    async def ainvoke(self, prompt, config=None):
        self.prompt = prompt
        return SimpleNamespace(content=self.content)


class _FakeBudget:
    async def take(self) -> None:
        return None


class _FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event, payload, goal_id=None):
        self.events.append((event, payload))


def test_analysis_prompt_keeps_clinical_boundaries():
    from meta_agent.flows.consultation_room.analysis import _build_prompt

    prompt = _build_prompt("脑卒中恢复期", "患者诉行走不稳", "分析一下最近的变化")
    assert "脑卒中恢复期" in prompt
    assert "患者诉行走不稳" in prompt
    assert "不给出诊断结论" in prompt


def test_analysis_subtask_emits_draft(monkeypatch):
    import meta_agent.flows.consultation_room.subtasks as subtasks

    async def fake_run_analysis(ctx, query, model):
        return "评估分析（草稿·待医生确认）"

    monkeypatch.setattr(subtasks, "run_analysis", fake_run_analysis)
    emitter = _FakeEmitter()
    ctx = SimpleNamespace(
        query="分析一下",
        record=SimpleNamespace(run_id="run_analysis", metrics={}, goal_statuses={}),
        emitter=emitter,
    )
    application = SimpleNamespace(agent_runtime=SimpleNamespace(model=object()))

    asyncio.run(subtasks._analysis(ctx, "评估分析", "", application))

    assert emitter.events[0][0] == "answer_part"
    assert "草稿" in emitter.events[0][1]["text"]
    assert ctx.record.goal_statuses["consultation_room:评估分析"] == "succeeded"


def test_summary_subtask_emits_draft_without_writing_clinical_data():
    model = _FakeModel("问诊纪要（草稿·待医生确认）\n主诉：头痛")
    emitter = _FakeEmitter()
    ctx = SimpleNamespace(
        query="总结一下",
        memory=SimpleNamespace(turns=[{"user": "我头痛三天了", "assistant": "有无发热？"}]),
        record=SimpleNamespace(run_id="run_summary", metrics={}, goal_statuses={}),
        llm_budget=_FakeBudget(),
        emitter=emitter,
    )
    application = SimpleNamespace(agent_runtime=SimpleNamespace(model=model))

    asyncio.run(_summary(ctx, "纪要总结", "", application))

    assert "我头痛三天了" in model.prompt
    assert "只使用对话中已经出现的信息" in model.prompt
    assert emitter.events[0][0] == "answer_part"
    assert "草稿" in emitter.events[0][1]["text"]
    assert ctx.record.metrics["summary_generated"] is True
    assert ctx.record.goal_statuses["consultation_room:纪要总结"] == "succeeded"
