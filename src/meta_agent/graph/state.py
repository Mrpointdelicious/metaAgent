"""
创建日期：2026-08-29
文件功能：定义单次智能体运行的可序列化状态结构。
"""

from typing import Any, NotRequired, TypedDict


class AgentState(TypedDict):
    """仅保存编排所需小状态，不保存完整工具 JSON。"""

    query: str
    tenant_id: str
    end_user_id: str
    patient_id: str
    scope_hash: str
    conversation_id: str
    run_id: str
    tasks: NotRequired[list[str]]
    requested_output: NotRequired[str]
    result_ref: NotRequired[str]
    facts: NotRequired[dict[str, Any]]
    compact_context: NotRequired[dict[str, Any]]
    patient_message: NotRequired[str]
    response_text: NotRequired[str]
    image_urls: NotRequired[list[str]]
    mode_commands: NotRequired[list[str]]
