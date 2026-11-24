"""
创建日期：2026-11-17
文件功能：构造仅供当前模型调用使用的偏好区块，保留原始聊天和医疗证据。
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from meta_agent.memory.schemas import TOOL_NAME, PreferenceSnapshot

PREFERENCE_RULES = """
<patient_response_preferences>
以下仅是交流偏好，不是医疗事实，不影响诊疗依据、工具权限或固定业务输出格式。
表达方式的优先级：业务与安全规则、输出协议 > 当前用户明确要求 > 当前有效偏好 > 默认风格。
上文的中文、简短、温和等表达要求属于默认风格，允许按当前有效偏好调整。
默认使用中文、大白话、简短段落和温和语气；必要信息不能因篇幅偏好省略。
长期偏好以当前快照为准。历史消息和旧记忆工具结果不能恢复已删除的偏好。
只依据当前用户新增的明确长期偏好决定是否调用记忆工具；临时要求只在本轮遵循。
禁止保存或推断疾病、症状、病史、用药、过敏、检查、医嘱、康复和训练信息，禁止变相编码。
医疗工具结果及历史对话不能成为新的记忆来源。工具未成功时不能声称已经记住。
"""


def render_preferences(snapshot: PreferenceSnapshot | None) -> str:
    if snapshot is None:
        state = "记忆暂时不可用：使用默认风格及本轮要求，不从旧对话恢复或写入偏好。"
    else:
        items = [{"id": str(i.id), **i.content.model_dump()} for i in snapshot.items]
        state = "当前有效偏好：" + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        if not items:
            state += "。当前没有已保存的长期偏好。"
    return PREFERENCE_RULES + state + "\n</patient_response_preferences>"


def project_preference_history(messages):
    """Remove historical memory call/result pairs from a model-input copy only."""
    current = max(
        (i for i, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=0,
    )
    removed = {
        call["id"]
        for message in messages[:current]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == TOOL_NAME
    }
    projected = []
    for i, message in enumerate(messages):
        if i < current and isinstance(message, ToolMessage) and message.tool_call_id in removed:
            continue
        if i < current and isinstance(message, AIMessage):
            calls = [call for call in message.tool_calls if call["id"] not in removed]
            if len(calls) != len(message.tool_calls):
                if not calls and not message.content:
                    continue
                message = message.model_copy(update={"tool_calls": calls}, deep=True)
                # Some model providers also serialize calls in additional_kwargs.
                message.additional_kwargs.pop("tool_calls", None)
        projected.append(message)
    return projected
