"""
创建日期：2026-11-11
文件功能：验证默认分支后处理——打开对话记录在单/多意图下对答复的不同处理。

回归背景：多意图指令（如「带我去四楼并介绍五楼并打开对话记录」）中三个工具
全部成功，但后处理曾把 LLM 答复整体覆盖成「已为您打开历史对话记录」，
导致五楼介绍丢失。规则应为：只有 open_chat_history 是本轮唯一有效结果时
才覆盖答复；有其他成功结果时保留 LLM 答复，仅前置 [web]。
"""

from types import SimpleNamespace

from meta_agent.flows.default.post_process import post_process

SPACE = "67ecd511-5532-405d-9b70-1cd1c0532eed"
WEB = "[web]http://192.168.8.160:9090/chatPage/?user=168&spaceId=" + SPACE
HISTORY_MESSAGE = "已为您打开历史对话记录。"


def make_ctx():
    return SimpleNamespace(record=SimpleNamespace(raw_inputs={}))


def history_item(**overrides):
    item = {
        "task_id": "t3",
        "tool": "open_chat_history",
        "status": "succeeded",
        "message": HISTORY_MESSAGE,
        "arguments": {},
        "facts": [],
        "raw": {"legacy_web": WEB, "patient_message": HISTORY_MESSAGE, "web_url": WEB[5:]},
    }
    item.update(overrides)
    return item


def knowledge_item(**overrides):
    item = {
        "task_id": "t2",
        "tool": "search_knowledge",
        "status": "succeeded",
        "message": "五楼是康复训练区……",
        "arguments": {"domain": "product", "query": "五楼"},
        "facts": [],
        "raw": {"success": "true", "answer": "五楼是康复训练区……"},
    }
    item.update(overrides)
    return item


def profile_item():
    return {
        "task_id": "t1",
        "tool": "get_patient_profile",
        "status": "succeeded",
        "message": "",
        "arguments": {},
        "facts": [],
        "raw": {},
    }


def test_single_intent_answer_replaced():
    collected = [profile_item(), history_item()]
    out = post_process(make_ctx(), "LLM 自由发挥", collected)
    assert out["command"] == WEB
    assert "[Answer:]" + HISTORY_MESSAGE in out["answer"]


def test_multi_intent_answer_kept():
    collected = [
        profile_item(),
        knowledge_item(),
        {
            "task_id": "t4",
            "tool": "navigate_scene",
            "status": "succeeded",
            "message": "",
            "arguments": {},
            "facts": [],
            "raw": {"success": "true", "scene_point": "23"},
        },
        history_item(),
    ]
    llm_answer = "已带您前往四楼。五楼是康复训练区，设有……已为您打开历史对话记录。"
    out = post_process(make_ctx(), llm_answer, collected)
    assert out["command"] == WEB
    assert "[web]" in out["answer"]
    assert "[Answer:]" + llm_answer in out["answer"]
    assert HISTORY_MESSAGE not in out["answer"].split("[Answer:]")[0] + "[Answer:]"


def test_multi_intent_other_tool_failed_still_replaced():
    collected = [knowledge_item(status="failed"), history_item()]
    out = post_process(make_ctx(), "LLM 自由发挥", collected)
    assert "[Answer:]" + HISTORY_MESSAGE in out["answer"]


def test_profile_only_does_not_count_as_other_result():
    collected = [profile_item(), history_item()]
    out = post_process(make_ctx(), "LLM 自由发挥", collected)
    assert "[Answer:]" + HISTORY_MESSAGE in out["answer"]


def test_history_tool_failed_keeps_llm_answer():
    collected = [history_item(status="failed", raw={})]
    out = post_process(make_ctx(), "抱歉，没能打开对话记录。", collected)
    assert out["command"] == ""
    assert "[Answer:]抱歉，没能打开对话记录。" in out["answer"]
