"""
创建日期：2026-09-12
文件功能：验证 Dify chat-messages 兼容接口、作用域隔离鉴权以及未启用的 workflow 框架。
"""

import json
from types import SimpleNamespace

import pytest

from meta_agent.adapters.dify import DifyEventAdapter, split_answer_segments
from meta_agent.contracts import OutboundEvent, RunRecord, TaskResult
from meta_agent.flows.default.post_process import post_process


def payload(query="解读最近训练", request_id="req-1", **kwargs):
    return {
        "query": query,
        "request_id": request_id,
        "user": "actor",
        "conversation_id": "conversation",
        "inputs": {"patientId": "461"},
        "response_mode": "blocking",
        **kwargs,
    }


def test_chat_messages_blocking(client, auth_headers):
    response = client.post(
        "/v1/chat-messages", headers=auth_headers, json=payload()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event"] == "message"
    assert body["mode"] == "chat"
    assert body["conversation_id"] == "conversation"
    assert body["message_id"] == body["id"]
    assert body["task_id"] != body["message_id"]
    assert "0.5 m/s" in body["answer"]
    assert body["metadata"]["retriever_resources"] == []


def test_chat_messages_streaming(client, auth_headers):
    response = client.post(
        "/v1/chat-messages",
        headers=auth_headers,
        json=payload("你好", response_mode="streaming", inputs={}),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    events = [f["event"] for f in frames]
    assert events[0] == "workflow_started"
    message = frames[events.index("message")]
    assert "你好" in message["answer"]
    assert events[-1] == "message_end"
    assert events[-2] == "workflow_finished"
    for frame in frames:
        assert frame["task_id"] == frames[0]["task_id"]
        assert frame["message_id"] == frames[0]["message_id"]
        assert frame["conversation_id"] == "conversation"
    assert frames[0]["message_id"] != frames[0]["task_id"]


def test_dify_adapter_emits_workflow_lifecycle_events():
    record = RunRecord(
        run_id="run-1",
        request_id="req-1",
        request_hash="h",
        scope_key="s",
        conversation_id="conv-1",
    )
    record.results["agent_tool_1"] = TaskResult(
        task_id="agent_tool_1",
        status="succeeded",
        outputs={"tool_name": "get_patient_profile"},
    )
    adapter = DifyEventAdapter(record)

    def ev(seq, type_, task_id=None, payload=None):
        return OutboundEvent(
            request_id="req-1",
            run_id="run-1",
            conversation_id="conv-1",
            seq=seq,
            type=type_,
            task_id=task_id,
            payload=payload or {},
        )

    frames = [
        adapter.encode(ev(1, "accepted", payload={"text": "收到。"})),
        adapter.encode(
            ev(2, "progress", task_id="agent_tool_1", payload={"stage": "tool", "text": "..."})
        ),
        adapter.encode(
            ev(
                3,
                "answer_part",
                payload={"text": "答", "fact_ids": [], "doc_refs": [], "revision": 1, "replaces": None},
            )
        ),
        adapter.encode(
            ev(
                4,
                "completed",
                payload={
                    "outcome": "succeeded",
                    "goal_statuses": {},
                    "task_statuses": {},
                    "event_range": {"first_seq": 1, "last_seq": 4},
                },
            )
        ),
    ]
    events = [
        json.loads(line[6:])
        for line in "".join(frames).splitlines()
        if line.startswith("data: ")
    ]
    names = [e["event"] for e in events]
    assert names == [
        "workflow_started",
        "node_started",
        "node_finished",
        "message",
        "workflow_finished",
        "message_end",
    ]
    node = events[names.index("node_finished")]
    assert node["data"]["status"] == "succeeded"
    assert node["data"]["node_id"] == "agent_tool_1"
    assert node["data"]["title"] == "get_patient_profile"


def _adapter_frames(events):
    record = RunRecord(
        run_id="run-1",
        request_id="req-1",
        request_hash="h",
        scope_key="s",
        conversation_id="conv-1",
    )
    adapter = DifyEventAdapter(record)

    def ev(seq, type_, payload=None):
        return OutboundEvent(
            request_id="req-1",
            run_id="run-1",
            conversation_id="conv-1",
            seq=seq,
            type=type_,
            payload=payload or {},
        )

    frames = "".join(adapter.encode(ev(i + 1, t, p)) for i, (t, p) in enumerate(events))
    return [
        json.loads(line[6:])
        for line in frames.splitlines()
        if line.startswith("data: ")
    ]


def test_dify_adapter_splits_bracketed_answer_into_messages():
    text = "[timestamp=150.26][ScenePoint:]5[ClickPoint:]1[Answer:]好的。"
    events = _adapter_frames(
        [
            ("accepted", {"text": "收到。"}),
            (
                "answer_part",
                {"text": text, "fact_ids": [], "doc_refs": [], "revision": 1, "replaces": None},
            ),
            (
                "completed",
                {
                    "outcome": "succeeded",
                    "goal_statuses": {},
                    "task_statuses": {},
                    "event_range": {"first_seq": 1, "last_seq": 3},
                },
            ),
        ]
    )
    assert [e["event"] for e in events] == [
        "workflow_started",
        "message",
        "message",
        "message",
        "message",
        "workflow_finished",
        "message_end",
    ]
    assert [e["answer"] for e in events if e["event"] == "message"] == [
        "[timestamp=150.26]",
        "[ScenePoint:]5",
        "[ClickPoint:]1",
        "[Answer:]好的。",
    ]


def test_dify_adapter_keeps_json_answer_as_single_message():
    text = '{"event":"end_consultation","display_text":"[Answer:]是否结束本次问诊？\\n","payload":{}}'
    events = _adapter_frames(
        [
            ("accepted", {"text": "收到。"}),
            (
                "answer_part",
                {"text": text, "fact_ids": [], "doc_refs": [], "revision": 1, "replaces": None},
            ),
            (
                "completed",
                {
                    "outcome": "succeeded",
                    "goal_statuses": {},
                    "task_statuses": {},
                    "event_range": {"first_seq": 1, "last_seq": 3},
                },
            ),
        ]
    )
    assert [e["answer"] for e in events if e["event"] == "message"] == [text]


def test_workflow_finished_includes_full_answer():
    text = "[timestamp=150.26][ScenePoint:]5[ClickPoint:]1[Answer:]好的。"
    events = _adapter_frames(
        [
            ("accepted", {"text": "收到。"}),
            (
                "answer_part",
                {"text": text, "fact_ids": [], "doc_refs": [], "revision": 1, "replaces": None},
            ),
            (
                "completed",
                {
                    "outcome": "succeeded",
                    "goal_statuses": {},
                    "task_statuses": {},
                    "event_range": {"first_seq": 1, "last_seq": 3},
                },
            ),
        ]
    )
    finished = next(e for e in events if e["event"] == "workflow_finished")
    assert finished["data"]["outputs"] == {"answer": text}
    assert finished["data"]["error"] == ""


def test_post_process_prepends_timestamp_from_inputs():
    # 场景指令 [ScenePoint:] 现于工具返回时作为独立 message 实时下发（见
    # agent.tools.invoke_tool），post_process 不再把它拼进最终答复。
    collected = [
        {"tool": "resolve_scene_instruction", "raw": {"command": "[ScenePoint:]5[ClickPoint:]1"}}
    ]
    ctx = SimpleNamespace(record=SimpleNamespace(raw_inputs={"timestamp": 150.26}))
    result = post_process(ctx, "好的。", collected)
    assert result["answer"] == "[timestamp=150.26][Answer:]好的。"
    assert result["timestamp"] == "150.26"


def test_post_process_omits_timestamp_when_absent():
    ctx = SimpleNamespace(record=SimpleNamespace(raw_inputs={}))
    result = post_process(ctx, "好的。", [])
    assert result["answer"] == "[Answer:]好的。"
    assert result["timestamp"] == ""


def test_post_process_skips_timestamp_when_disabled():
    # 场景指令 [ScenePoint:] 现于工具返回时独立下发，post_process 不再拼进 answer。
    collected = [
        {"tool": "resolve_scene_instruction", "raw": {"command": "[ScenePoint:]5[ClickPoint:]1"}}
    ]
    ctx = SimpleNamespace(record=SimpleNamespace(raw_inputs={"timestamp": 150.26}))
    result = post_process(ctx, "好的。", collected, include_timestamp=False)
    assert result["answer"] == "[Answer:]好的。"
    assert result["timestamp"] == ""


def test_post_process_chat_history_outputs_web_link_with_answer():
    web = "[web]http://192.168.8.160:9090/chatPage/?user=118&spaceId=67ecd511-5532-405d-9b70-1cd1c0532eed"
    collected = [
        {
            "tool": "open_chat_history",
            "raw": {"legacy_web": web, "web_url": web[5:], "patient_message": "已为您打开历史对话记录。"},
        }
    ]
    ctx = SimpleNamespace(record=SimpleNamespace(raw_inputs={"timestamp": 150.26}))
    # LLM 可能把网址也拼进答案；post_process 应改用工具的 patient_message，避免重复 web 链接。
    result = post_process(ctx, "已为您打开历史对话记录，可点击查看：" + web, collected)
    assert result["answer"] == "[timestamp=150.26]" + web + "[Answer:]已为您打开历史对话记录。"
    assert result["command"] == web


def _report_ctx(timestamp=None):
    return SimpleNamespace(
        record=SimpleNamespace(raw_inputs={"timestamp": timestamp} if timestamp else {}),
        scope=SimpleNamespace(
            end_user_id="130", space_id="67ecd511-5532-405d-9b70-1cd1c0532eed"
        ),
    )


# 报表 [web] 地址与「打开历史对话记录」一致，复用 chat_history.build。
WEB_PAGE = (
    "[web]http://192.168.8.161:9090/chatPage/?user=130"
    "&spaceId=67ecd511-5532-405d-9b70-1cd1c0532eed"
)


def test_post_process_report_prepends_cweb_web_and_short_answer():
    image_url = "http://192.168.8.160:15043/api/ai/patients/report-artifacts/irego_img_v2_abc/content"
    collected = [
        {
            "tool": "generate_irego_single_session_report",
            "raw": {"data": {"artifact_ref": "irego_img_v2_abc", "image_url": image_url}},
        }
    ]
    # LLM 可能把图片拼成 markdown；post_process 应改用短句，并前置 [Cweb] 图 + [web] 页。
    result = post_process(_report_ctx("251841.70"), "报表已生成：\n\n![单次训练报告](http://x/img)", collected)
    assert result["answer"] == (
        "[timestamp=251841.70][Cweb]"
        + image_url
        + WEB_PAGE
        + "[Answer:]单次训练报告已生成，请查看下方图片。"
    )
    assert result["Cweb"] == "[Cweb]" + image_url
    assert result["web"] == WEB_PAGE


def test_post_process_longitudinal_report_uses_trend_message():
    collected = [
        {
            "tool": "generate_irego_longitudinal_report",
            "raw": {"data": {"image_url": "http://x/trend.png"}},
        }
    ]
    result = post_process(_report_ctx(), "长答复", collected)
    assert result["answer"] == (
        "[Cweb]http://x/trend.png"
        + WEB_PAGE
        + "[Answer:]连续训练趋势报告已生成，请查看下方图片。"
    )


def test_post_process_report_omits_web_when_identity_missing():
    collected = [
        {
            "tool": "generate_irego_single_session_report",
            "raw": {"data": {"image_url": "http://x/img"}},
        }
    ]
    ctx = SimpleNamespace(
        record=SimpleNamespace(raw_inputs={}),
        scope=SimpleNamespace(end_user_id="", space_id=""),
    )
    result = post_process(ctx, "长答复", collected)
    assert result["answer"] == "[Cweb]http://x/img[Answer:]单次训练报告已生成，请查看下方图片。"
    assert result["web"] == ""


def test_split_answer_web_link_into_separate_message():
    web = "[web]http://192.168.8.160:9090/chatPage/?user=118&spaceId=67ecd511-5532-405d-9b70-1cd1c0532eed"
    text = "[timestamp=150.26]" + web + "[Answer:]已为您打开历史对话记录。"
    assert split_answer_segments(text) == [
        "[timestamp=150.26]",
        web,
        "[Answer:]已为您打开历史对话记录。",
    ]


def test_scope_protects_status_and_cancel(client, auth_headers):
    own = client.post("/v1/chat", headers=auth_headers, json=payload()).json()
    access = {"user": "actor", "inputs": {"patientId": "461"}}
    path = f"/v1/runs/{own['task_id']}"
    assert client.post(path + "/status", headers=auth_headers, json=access).status_code == 200
    for changed in (
        {"patientId": "462"},
        {"patientId": "461", "role": "clinician"},
        {"patientId": "461", "tenantId": "another"},
    ):
        for endpoint in ("/status", "/cancel"):
            assert (
                client.post(
                    path + endpoint, headers=auth_headers, json={**access, "inputs": changed}
                ).status_code
                == 404
            )
    other = client.post(
        "/v1/chat", headers=auth_headers, json=payload(inputs={"patientId": "462"})
    ).json()
    assert other["task_id"] != own["task_id"]


def test_cors_headers_returned_for_browser(client, auth_headers):
    origin = "http://192.168.8.160:8088"
    resp = client.get("/v1/conversations", params={"user": "actor"}, headers={**auth_headers, "Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"

    preflight = client.options(
        "/v1/conversations",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == "*"
    assert preflight.headers.get("access-control-allow-methods")


@pytest.mark.parametrize(
    "endpoint",
    [
        "/v1/chat",
        "/v1/chat-messages",
        "/compat/dify/v1/chat-messages",
        "/compat/dify/v1/workflows/run",
    ],
)
def test_authentication(client, endpoint):
    assert client.post(endpoint, json=payload()).status_code == 401


def test_workflows_run_is_explicitly_deferred(client, auth_headers):
    assert (
        client.post(
            "/compat/dify/v1/workflows/run", headers=auth_headers, json=payload()
        ).status_code
        == 501
    )


def test_optional_patient_and_mixed_identity(client, auth_headers):
    resp = client.post(
        "/v1/chat",
        headers=auth_headers,
        json=payload("查询在线医生", inputs={"spaceId": "demo-space"}),
    ).json()
    status = client.post(
        f"/v1/runs/{resp['task_id']}/status",
        headers=auth_headers,
        json={"user": "actor", "inputs": {"spaceId": "demo-space"}},
    ).json()
    assert status["status"] == "succeeded"
    assert status["metrics"]["tool_calls"] == 1

    resp = client.post(
        "/v1/chat",
        headers=auth_headers,
        json=payload(
            "查一下患者信息", "req-2", inputs={"patientId": {"type": "mixed", "value": "461"}}
        ),
    ).json()
    status = client.post(
        f"/v1/runs/{resp['task_id']}/status",
        headers=auth_headers,
        json={"user": "actor", "inputs": {"patientId": {"type": "mixed", "value": "461"}}},
    ).json()
    assert status["status"] == "unsupported"
    assert status["metrics"]["tool_calls"] == 0
