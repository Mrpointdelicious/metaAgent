"""
创建日期：2026-11-03
文件功能：在读取任何历史之前选择并绑定普通或问诊独立会话。
"""

from dataclasses import replace
from typing import Literal

from pydantic import ConfigDict, Field

from meta_agent.contracts import DomainError, StrictModel, fingerprint, identifier, utcnow
from meta_agent.domains.routing import route_from_inputs
from meta_agent.infrastructure.locks import ThreadLockRegistry


class ConversationBinding(StrictModel):
    model_config = ConfigDict(frozen=True)
    conversation_id: str
    owner: str
    authorization_scope: str
    agent_kind: Literal["default", "consultation_room"]
    consultation_context: str = ""
    authorized_patient_binding: str = ""
    consultation_id: str = ""
    position: str = ""
    origin_default_conversation_id: str = ""
    source_app: str = ""
    caller_conversation_id: str = ""
    created_at: float = Field(default_factory=lambda: utcnow().timestamp())


def owner_key(scope):
    # A service credential identifies a gateway, never a patient or clinician.
    return fingerprint([scope.tenant_id, scope.end_user_id, scope.role, scope.client_id])


def public_inputs(inputs):
    """Persist routing metadata only; credentials and patient lookup keys stay runtime-only."""
    allowed = {"pos", "timestamp", "spaceId", "space_id", "sceneVersion", "scene_version"}
    return {k: v for k, v in inputs.items() if k in allowed and isinstance(v, (str, int, float))}


class ConversationSelector:
    def __init__(self, repository):
        self.repository = repository
        self.locks = ThreadLockRegistry()

    async def get(self, scope, conversation_id):
        data = await self.repository.get(
            "conversation_bindings_v2", owner_key(scope), conversation_id
        )
        return ConversationBinding.model_validate(data) if data else None

    async def save(self, binding):
        await self.repository.put(
            "conversation_bindings_v2",
            binding.owner,
            binding.conversation_id,
            binding.model_dump(mode="json"),
            self.repository.settings.conversation_ttl_seconds,
        )

    async def select(self, request):
        inputs, scope = request.raw_inputs, request.scope
        if request.client is not None:
            scope = replace(scope, client_id=request.client.name)
        try:
            kind = route_from_inputs(inputs).name
        except ValueError as exc:
            raise DomainError("invalid_position", str(exc)) from exc
        position = inputs.get("pos", "").strip()
        business_id = inputs.get("consultation_id", "")
        source_app = inputs.get("source_app", "")
        caller_id = inputs.get("caller_conversation_id", "")
        if any(
            not isinstance(v, str) or len(v) > 256 for v in (business_id, source_app, caller_id)
        ):
            raise DomainError("invalid_conversation_context", "会话上下文字段无效。")
        if bool(source_app) != bool(caller_id):
            raise DomainError("invalid_conversation_context", "桥接应用和外层会话必须同时提供。")
        if source_app and not position:
            raise DomainError("bridge_position_required", "桥接请求必须提供当前位置。")
        scope = replace(scope, position=position, consultation_id=business_id)
        patient = scope.consultation_patient_ref or scope.patient_ref or ""
        context = (
            fingerprint([position, business_id, patient]) if kind == "consultation_room" else ""
        )
        owner = owner_key(scope)
        async with self.locks.hold(owner):
            binding = (
                await self.get(scope, request.conversation_id) if request.conversation_id else None
            )
            # Direct requests may only resume a registered ID. Native opaque IDs
            # remain a compatibility alias in the new namespace, without old state.
            if (
                request.conversation_id
                and binding is None
                and not source_app
                and (
                    request.protocol != "native"
                    or request.conversation_id.startswith("conversation_v2_")
                )
            ):
                raise DomainError("conversation_not_found", "会话不存在或不属于当前用户。")
            if binding and binding.agent_kind == "consultation_room" and not position:
                raise DomainError("consultation_position_required", "问诊续聊缺少当前位置。")
            alias_key = fingerprint([source_app, caller_id, kind, context, scope.scope_hash])
            if source_app:
                alias = await self.repository.get("conversation_aliases_v2", owner, alias_key)
                if alias:
                    binding = await self.get(scope, alias["conversation_id"])
            origin = ""
            if binding and binding.authorization_scope == scope.scope_hash:
                if binding.agent_kind == kind and binding.consultation_context == context:
                    await self.save(binding)
                    return binding, replace(scope, agent_kind=kind, consultation_context=context)
                if binding.agent_kind == "default" and kind == "consultation_room":
                    origin = binding.conversation_id
                elif kind == "default" and binding.origin_default_conversation_id:
                    previous = await self.get(scope, binding.origin_default_conversation_id)
                    if (
                        previous
                        and previous.authorization_scope == scope.scope_hash
                        and previous.agent_kind == "default"
                    ):
                        await self.save(previous)
                        return previous, replace(
                            scope, agent_kind="default", consultation_context=""
                        )
            cid = identifier("conversation_v2")
            if (
                request.protocol == "native"
                and request.conversation_id
                and binding is None
                and not source_app
            ):
                cid = request.conversation_id
            binding = ConversationBinding(
                conversation_id=cid,
                owner=owner,
                authorization_scope=scope.scope_hash,
                agent_kind=kind,
                consultation_context=context,
                authorized_patient_binding=patient if kind == "consultation_room" else "",
                consultation_id=business_id,
                position=position,
                origin_default_conversation_id=origin,
                source_app=source_app,
                caller_conversation_id=caller_id,
            )
            await self.save(binding)
            if source_app:
                await self.repository.put(
                    "conversation_aliases_v2",
                    owner,
                    alias_key,
                    {"conversation_id": cid},
                    self.repository.settings.conversation_ttl_seconds,
                )
            return binding, replace(scope, agent_kind=kind, consultation_context=context)
