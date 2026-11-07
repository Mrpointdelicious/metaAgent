"""
创建日期：2026-11-07
文件功能：核验旧会话归属并提取可迁移的普通对话，不恢复旧工具或临床状态。
"""

import json
from hashlib import sha256
from math import isfinite

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict

from meta_agent.contracts import ConversationState
from meta_agent.domains.routing import route_from_inputs
from meta_agent.orchestration.identity import LEGACY_PATIENT_KEYS, trusted_scope_from_inputs


def legacy_thread_id(scope, conversation_id):
    """The pre-v2 format for a normal conversation with no bound patient."""
    material = json.dumps(
        [scope.tenant_id, scope.end_user_id, scope.role, None],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    old_scope = sha256(material.encode()).hexdigest()
    return sha256(
        json.dumps([old_scope, conversation_id], separators=(",", ":")).encode()
    ).hexdigest()


async def load_legacy_conversation(repository, scope, conversation_id):
    # Old indexes did not record the gateway. Its owner must be declared by the
    # deployment, never inferred from whichever client tries the ID first.
    if (
        repository.settings.legacy_conversation_client != scope.client_id
        or conversation_id.startswith("conversation_v2_")
        or scope.patient_identity is not None
        or scope.patient_ref is not None
        or scope.data_scope != "all_tenants"
        or scope.current_business_id is not None
    ):
        return None
    meta = await repository.conversation_meta(scope.end_user_id, conversation_id)
    if not meta or meta.get("conversation_id") != conversation_id:
        return None
    if not isinstance(meta.get("name"), str) or any(
        type(meta.get(key)) not in (int, float) or not isfinite(meta[key]) or meta[key] < 0
        for key in ("created_at", "updated_at")
    ):
        return None
    inputs = meta.get("inputs")
    if not isinstance(inputs, dict):
        return None
    if any(inputs.get(key) not in (None, "") for key in LEGACY_PATIENT_KEYS):
        return None
    try:
        if route_from_inputs(inputs).name != "default":
            return None
        old_inputs = {k: v for k, v in inputs.items() if k not in LEGACY_PATIENT_KEYS}
        # The old HTTP adapter gave identity precedence over role. Reconstruct
        # that original scope only for verifying its stored thread fingerprint.
        old_role = {
            "医生": "clinician",
            "doctor": "clinician",
            "clinician": "clinician",
            "患者": "patient",
            "patient": "patient",
        }.get(inputs.get("identity"))
        if old_role:
            old_inputs["role"] = old_role
        old_scope = trusted_scope_from_inputs(
            old_inputs,
            scope.end_user_id,
            repository.settings.default_tenant_id,
        )
    except (ValueError, TypeError, AttributeError):
        return None
    if (
        old_scope.tenant_id != scope.tenant_id
        or old_scope.role != scope.role
        or old_scope.patient_identity is not None
        or meta.get("thread_id") != legacy_thread_id(scope, conversation_id)
    ):
        return None
    raw = await repository.get("conversations", meta["thread_id"], "state")
    if raw is None:
        return None
    try:
        previous = ConversationState.model_validate(raw)
    except ValueError:
        return None
    # An old consultation cannot establish separate room/target authorization.
    if previous.previous_category or previous.variables or previous.current_record:
        return None
    turns, messages = [], []
    for turn in previous.turns:
        if not all(isinstance(turn.get(key), str) for key in ("user", "assistant")):
            return None
        turns.append({k: turn[k] for k in ("user", "assistant", "created_at") if k in turn})
        messages.extend([HumanMessage(turn["user"]), AIMessage(turn["assistant"])])
    # Tool results, old evidence, pending actions and arbitrary variables are not
    # transferable authorization. Only the verified owner's dialogue is copied.
    return meta, ConversationState(turns=turns, agent_messages=messages_to_dict(messages))
