"""
创建日期：2026-10-16
文件功能：验证打开历史对话记录工具（拼 URL + 意图识别）。
"""

import asyncio
from types import SimpleNamespace

import pytest

from meta_agent.contracts import TaskSpec
from meta_agent.domains.chat_history import ChatHistoryAdapter, build, is_chat_history_query

SPACE = "67ecd511-5532-405d-9b70-1cd1c0532eed"
URL = "http://192.168.8.161:9090/chatPage/?user=118&spaceId=" + SPACE


def test_build_success():
    out = build("118", SPACE)
    assert out["success"] == "true"
    assert out["status"] == "success"
    assert out["action"] == "open_chat_history"
    assert out["web_url"] == URL
    assert out["legacy_web"] == "[web]" + URL
    assert out["error"] == ""


def test_build_missing_user():
    out = build("", SPACE)
    assert out["success"] == "false"
    assert out["error"] == "user_id required"
    assert out["web_url"] == ""
    assert out["legacy_web"] == ""


def test_build_missing_space():
    out = build("118", "")
    assert out["success"] == "false"
    assert out["error"] == "space_id required"
    assert out["web_url"] == ""


@pytest.mark.parametrize(
    "query",
    ["打开对话记录", "查看对话记录", "看聊天记录", "打开历史对话", "查看聊天历史"],
)
def test_recognizes_chat_history(query):
    assert is_chat_history_query(query) is True


@pytest.mark.parametrize(
    "query",
    ["打开摄像头", "打开个人中心", "去二楼", "你好", "我要挂号", ""],
)
def test_does_not_misread_other_commands(query):
    assert is_chat_history_query(query) is False


def test_adapter_execute_success():
    saved = {}

    async def save_evidence(scope, tool, request_id, payload, version):
        saved.update(scope=scope, tool=tool, payload=payload, version=version)
        return SimpleNamespace(evidence_id="evt-1")

    ctx = SimpleNamespace(
        scope=SimpleNamespace(end_user_id="118", space_id=SPACE, scope_hash="h"),
        record=SimpleNamespace(request_id="req-1", run_id="run-1"),
        repository=SimpleNamespace(save_evidence=save_evidence),
    )
    task = TaskSpec(
        task_id="t1", goal_ids=["agent"], capability="chat.history", arguments={}, idempotency_key="k"
    )
    result = asyncio.run(ChatHistoryAdapter().execute(task, {}, ctx))
    assert result.status == "succeeded"
    assert result.message == "已为您打开历史对话记录。"
    assert result.outputs["command"] == "[web]" + URL
    assert saved["tool"] == "open_chat_history"
    assert saved["payload"]["legacy_web"] == "[web]" + URL
