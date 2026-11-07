"""
创建日期：2026-11-03
文件功能：在读取任何历史之前选择并绑定普通或问诊独立会话。
"""

from dataclasses import replace
from typing import Literal

from pydantic import ConfigDict, Field

from meta_agent.application.legacy_conversations import load_legacy_conversation
from meta_agent.contracts import DomainError, StrictModel, fingerprint, identifier, utcnow
from meta_agent.domains.routing import route_from_inputs
from meta_agent.infrastructure.locks import ThreadLockRegistry
from meta_agent.orchestration.identity import space_id_from_inputs


class ConversationBinding(StrictModel):
    model_config = ConfigDict(frozen=True)
    conversation_id: str
    owner: str
    authorization_scope: str
    agent_kind: Literal["default", "consultation_room"]
    consultation_context: str = ""
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
    allowed = {
        "pos",
        "timestamp",
        "spaceId",
        "space_id",
        "spaceid",
        "sceneVersion",
        "scene_version",
    }
    result = {k: v for k, v in inputs.items() if k in allowed and isinstance(v, (str, int, float))}
    space = space_id_from_inputs(inputs)
    if space is not None:
        result["space_id"] = space
    return result


def consultation_inputs(inputs):
    """Keep clinical lookup fields runtime-only, outside patient v2 identity."""
    values = {}
    for key, aliases in (
        ("patientphone", ("patientphone", "patientPhone", "patient_phone", "phone")),
        ("requesterphone", ("requesterphone", "requesterPhone", "requester_phone", "phone")),
    ):
        value = next((inputs[name] for name in aliases if inputs.get(name) not in (None, "")), None)
        if value is not None:
            values[key] = str(value)
    return values


class ConversationSelector:
    def __init__(self, repository):
        self.repository = repository
        self.locks = ThreadLockRegistry()

    async def get(self, scope, conversation_id):
        data = await self.repository.get(
            "conversation_bindings_v2", owner_key(scope), conversation_id
        )
        if data:
            # Read bindings written before the temporary provider was withdrawn.
            data = {
                key: value for key, value in data.items() if key != "authorized_patient_binding"
            }
        return ConversationBinding.model_validate(data) if data else None

    async def save(self, binding):
        await self.repository.put(
            "conversation_bindings_v2",
            binding.owner,
            binding.conversation_id,
            binding.model_dump(mode="json"),
            self.repository.settings.conversation_ttl_seconds,
        )

    async def _migrate_legacy(self, scope, conversation_id):
        source = await load_legacy_conversation(self.repository, scope, conversation_id)
        if source is None:
            return None
        meta, memory = source
        selected = replace(scope, agent_kind="default", consultation_context="")
        thread = selected.thread_id(conversation_id)
        # Do not overwrite newer history when resuming an interrupted migration.
        if await self.repository.get("conversations", thread, "state") is None:
            await self.repository.save_conversation(thread, memory)
        binding = ConversationBinding(
            conversation_id=conversation_id,
            owner=owner_key(scope),
            authorization_scope=scope.scope_hash,
            agent_kind="default",
            position=str(meta["inputs"].get("pos", "")),
            created_at=meta["created_at"],
        )
        await self.repository.put(
            "conversation_index",
            binding.owner,
            conversation_id,
            {**meta, "thread_id": thread, "inputs": public_inputs(meta["inputs"])},
            self.repository.settings.conversation_ttl_seconds,
        )
        await self.save(binding)
        return binding

    async def lookup(self, scope, conversation_id):
        async with self.locks.hold(owner_key(scope)):
            return await self.get(scope, conversation_id) or await self._migrate_legacy(
                scope, conversation_id
            )

    async def migrate_history(self, scope):
        if self.repository.settings.legacy_conversation_client == scope.client_id:
            for meta in await self.repository.list_conversations(scope.end_user_id):
                await self.lookup(scope, meta["conversation_id"])

    async def history_scope(self, scope, conversation_id=""):
        """Recover a unique recorded role within this user/tenant/gateway only."""
        candidates = []
        legacy = (
            await self.repository.list_conversations(scope.end_user_id)
            if self.repository.settings.legacy_conversation_client == scope.client_id
            else []
        )
        for role in ("patient", "clinician", "operator"):
            candidate = replace(scope, role=role)
            ids = (
                [conversation_id]
                if conversation_id
                else [
                    meta["conversation_id"]
                    for meta in await self.repository.list_conversations(owner_key(candidate))
                ]
                + [meta["conversation_id"] for meta in legacy]
            )
            for cid in ids:
                binding = await self.get(candidate, cid)
                if binding is not None:
                    matches = binding.authorization_scope == candidate.scope_hash
                else:
                    matches = (
                        await load_legacy_conversation(self.repository, candidate, cid) is not None
                    )
                if matches:
                    candidates.append(candidate)
                    break
        if len(candidates) > 1:
            raise DomainError(
                "history_identity_required",
                "该用户存在多个角色的历史，请提供当前 role 或 identity。",
            )
        return candidates[0] if candidates else scope

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
        target = consultation_inputs(inputs).get("patientphone", "")
        patient = scope.patient_ref or ""
        if target:
            patient = fingerprint([patient, target])
        context = (
            fingerprint([position, business_id, patient]) if kind == "consultation_room" else ""
        )
        owner = owner_key(scope)
        async with self.locks.hold(owner):
            binding = (
                await self.get(scope, request.conversation_id) if request.conversation_id else None
            )
            if binding is None and request.conversation_id and not source_app:
                binding = await self._migrate_legacy(scope, request.conversation_id)
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
