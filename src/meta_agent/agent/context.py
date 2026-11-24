"""
创建日期：2026-10-05
文件功能：压缩患者上下文，保留工具消息配对并检查证据时效。
"""

import json
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage, messages_to_dict

from meta_agent.context.budget import estimate_tokens
from meta_agent.roles.prompts import SYSTEM_PROMPT

BASIC_COMMAND_SYSTEM_PROMPT = """你是在线问诊房间的智能助理，负责本次问诊中的基础问答与界面指令。
先分清用户是「提问」还是「指令」：
- 提问（询问/介绍/解释/说明/是什么/怎么用，含界面元素、按钮、区域、功能，如“介绍左上角”“个人中心是什么”“这个按钮干什么用”）走知识库：先调用资料检索工具（search_knowledge，依据问题选 help/product/health 领域）作答，不要凭空编造机构或产品信息。
- 指令（明确的操作/导航/设备控制动词，如“打开个人中心”“去二楼”“投屏”“切换视角”“关闭XX”“退回上个场景”）调用场景动作执行工具（navigate_scene），将原话中的动作片段作为target；根据工具交付状态回复，不要自行生成指令编码。
“介绍”“解释”“说明”“是什么”“怎么用”是提问，不是指令，不要调用场景指令解析工具。
明确要求执行的场景动作调用 navigate_scene；resolve_scene_instruction 仅用于匹配预览，不表示执行。
匹配成功、发往前端与前端执行完成是不同状态，没有客户端确认时不声称已经执行完成。
只使用本轮可用的工具；回答简洁自然，最终只输出答复文本，不输出内部状态码、工具 JSON 或调试过程，不提及患者姓名。"""


def preference_aware_prompt(prompt: str) -> str:
    """Make built-in style defaults overridable without changing business/evidence rules."""
    if prompt == SYSTEM_PROMPT:
        return (
            prompt.replace(
                "用中文、大白话交流，像拉家常一样，多用“您”“咱们”。",
                "默认用中文、大白话交流；语言和语气按当前有效沟通偏好调整。",
            )
            .replace(
                "2. 简短清晰：每次回复尽量控制在3句话以内。",
                "2. 清晰表达：默认简短；篇幅和排版按当前有效沟通偏好调整。",
            )
            .replace(
                "3. 多鼓励：无论数据好坏，先肯定患者的努力。",
                "3. 默认温和、适当鼓励；明确偏好中性语气时客观直接表达。",
            )
        )
    if prompt == BASIC_COMMAND_SYSTEM_PROMPT:
        return prompt.replace("回答简洁自然", "默认回答简洁自然，按当前有效沟通偏好调整表达")
    return prompt


def compact_result(body: dict, budget: int) -> dict:
    """Keep an explicit truncation marker; full data remains in the evidence store."""
    omitted = False

    def shrink(value: Any, text_limit: int | None, list_limit: int | None):
        nonlocal omitted
        if isinstance(value, str) and text_limit is not None and len(value) > text_limit:
            omitted = True
            return value[:text_limit] + "（摘要截断，可缩小查询范围获取更多内容）"
        if isinstance(value, list):
            if list_limit is not None and len(value) > list_limit:
                omitted = True
            return [shrink(x, text_limit, list_limit) for x in value[:list_limit]]
        if isinstance(value, dict):
            return {
                k: shrink(
                    "报告已生成，请查看报告卡片。"
                    if isinstance(v, str) and "access_grant=" in v
                    else v,
                    text_limit,
                    list_limit,
                )
                for k, v in value.items()
                if k
                not in {
                    "display_name_private",
                    "phone",
                    "password",
                    "identity_context_token",
                    "access_grant",
                    "system_context",
                }
            }
        return value

    for text_limit, list_limit in [
        (None, None),
        (1600, 10),
        (800, 5),
        (400, 3),
        (200, 2),
        (100, 1),
    ]:
        result = shrink(body, text_limit, list_limit)
        if omitted:
            result["context_truncated"] = True
        if estimate_tokens(result) <= budget:
            return result
    return {
        "tool_name": body.get("tool_name"),
        "status": body.get("status"),
        "evidence_id": body.get("evidence_id"),
        "context_truncated": True,
        "patient_message": "查询结果过大，请减少page_size或缩小范围后重查。",
    }


def turn_groups(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    groups: list[list[BaseMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage) or not groups:
            groups.append([])
        groups[-1].append(message)
    return groups


def bounded_history(messages: list[BaseMessage], budget: int) -> list[BaseMessage]:
    groups = turn_groups(messages)[-6:]
    while (
        len(groups) > 1
        and estimate_tokens(messages_to_dict([m for g in groups for m in g])) > budget
    ):
        groups.pop(0)
    return [m for group in groups for m in group]


async def fresh_history(messages: list[BaseMessage], ctx) -> list[BaseMessage]:
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("tool_name") == "search_knowledge"
            and (payload.get("data") or {}).get("retrieval_trace_id")
        ):
            # Publication or qualification can change before the next conversational turn.
            # A live Store TTL is not proof that the source remains published.
            message.content = json.dumps(
                {
                    "tool_name": "search_knowledge",
                    "status": "expired",
                    "patient_message": "旧知识结果须按当前发布重新检索，不能直接作为本轮证据。",
                },
                ensure_ascii=False,
            )
            continue
        eid = payload.get("evidence_id") if isinstance(payload, dict) else None
        if eid and await ctx.repository.evidence(ctx.scope.scope_hash, eid) is None:
            message.content = json.dumps(
                {
                    "tool_name": payload.get("tool_name"),
                    "status": "expired",
                    "patient_message": "该查询证据已过期，请重新查询；旧记录不能作为当前事实。",
                },
                ensure_ascii=False,
            )
    return bounded_history(messages, ctx.settings.agent_history_tokens)
