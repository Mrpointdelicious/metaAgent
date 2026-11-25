"""
创建日期：2026-10-29
文件功能：规范化已认证患者身份与康复范围，隔离会话和短期凭证。
"""

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any, Literal

from pydantic import Field, StrictStr, field_validator

from meta_agent.contracts import DomainError, StrictModel
from meta_agent.orchestration.resources import ResourceGrant
from meta_agent.roles import USER_ROLES

FRONTEND_IDENTITIES = {
    "患者": "patient",
    "patient": "patient",
    "医生": "clinician",
    "doctor": "clinician",
    "clinician": "clinician",
    "学生": "student",
    "student": "student",
}


def user_role_from_inputs(inputs: dict[str, Any]) -> str:
    if "role" in inputs:
        role = inputs["role"]
        if role in USER_ROLES:
            return role
    elif "identity" in inputs:
        identity = inputs["identity"]
        if isinstance(identity, str) and identity in FRONTEND_IDENTITIES:
            return FRONTEND_IDENTITIES[identity]
    else:
        return "patient"
    raise ValueError("可信用户角色无效")


class PatientIdentity(StrictModel):
    type: Literal["project_user_id", "login_name"]
    value: StrictStr = Field(min_length=1, max_length=64)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value, info):
        if value != value.strip():
            raise ValueError("患者标识不能包含首尾空格")
        if info.data.get("type") == "project_user_id" and (
            not value.isascii() or not value.isdigit() or int(value) <= 0
        ):
            raise ValueError("project_user_id 必须为正整数字符串")
        return value


@dataclass(frozen=True, slots=True)
class TrustedScope:
    tenant_id: str
    end_user_id: str
    patient_identity: PatientIdentity | None = None
    role: str = "patient"
    space_id: str | None = None
    scene_version: int | None = None
    current_business_id: int | None = None
    data_scope: str = "all_tenants"
    # Runtime only: excluded from repr, identity hashes and checkpoints.
    identity_context_token: str = field(default="", repr=False, compare=False)
    patient_ref: str | None = None
    scope_fingerprint: str | None = None
    position: str | None = None
    identity_note: str | None = None
    client_id: str = "primary"
    agent_kind: str = "default"
    consultation_context: str = ""
    consultation_id: str = ""
    resource_grant: ResourceGrant | None = field(default=None, repr=False)
    client_flow: str = ""
    allowed_tools: frozenset[str] | None = field(default=None, repr=False)
    policy_scope: str = ""
    # 过渡期免签：require_identity_token=false 时置 True，允许无 v2 凭证的患者身份。
    # Runtime only: excluded from repr, identity hashes and checkpoints.
    allow_unsigned_patient_identity: bool = field(
        default=False, repr=False, compare=False, kw_only=True
    )

    @property
    def has_patient(self) -> bool:
        # 与 11-17 旧部署版口径一致：require_identity_token=false 时 allow_unsigned=True，
        # 患者身份由 WebApi 免签通道（system_context + patient_identity）在
        # resolve_patient_identity 时绑定；带凭证的调用方仍走强校验分支。
        return bool(
            self.patient_identity
            and (self.identity_context_token or self.allow_unsigned_patient_identity)
            and self.patient_ref
            and self.scope_fingerprint
            and self.role == "patient"
        )

    @property
    def scope_hash(self) -> str:
        material = json.dumps(
            [
                self.tenant_id,
                self.end_user_id,
                self.role,
                self.patient_identity.model_dump() if self.patient_identity else None,
                self.data_scope,
                self.current_business_id if self.data_scope == "current_tenant" else None,
                self.patient_ref,
                self.scope_fingerprint,
                self.client_id,
                *([self.resource_grant.scope_key()] if self.resource_grant else []),
                *([self.policy_scope] if self.policy_scope else []),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(material.encode()).hexdigest()

    def thread_id(self, conversation_id: str) -> str:
        return sha256(
            json.dumps(
                [
                    "conversation-v2",
                    self.scope_hash,
                    self.agent_kind,
                    self.consultation_context,
                    conversation_id,
                ]
            ).encode()
        ).hexdigest()

    def patient_payload(self, arguments: dict, conversation_id: str = "") -> dict:
        if (
            not self.patient_identity
            or (not self.identity_context_token and not self.allow_unsigned_patient_identity)
            or self.role != "patient"
        ):
            raise DomainError(
                "patient_required", "请通过登录入口确认患者身份。", outcome="unavailable"
            )
        system_context = {
            "user": self.end_user_id,
            "conversation_id": conversation_id,
            "patient_identity": self.patient_identity.model_dump(),
            "current_business_id": self.current_business_id,
        }
        # 免签通道（allow_unsigned_patient_identity）不需要 token；带凭证的
        # 调用方仍可透传，走强校验分支。两者互不冲突。
        if self.identity_context_token:
            system_context["identity_context_token"] = self.identity_context_token
        return {
            **arguments,
            "system_context": system_context,
            "data_scope": self.data_scope,
        }

    def bind(self, body: dict) -> "TrustedScope":
        validate_patient_envelope(self, "resolve_patient_identity", body, binding=True)
        data, query = body["data"], body["query_scope"]
        if body["status"] not in {"success", "partial"} or data.get("binding_status") != "resolved":
            raise DomainError("identity_unresolved", "无法确认患者身份。", outcome="unavailable")
        return replace(
            self, patient_ref=data["patient_ref"], scope_fingerprint=query["scope_fingerprint"]
        )


def validate_patient_envelope(scope: TrustedScope, endpoint: str, body: dict, *, binding=False):
    if body.get("tool_name") != endpoint or body.get("contract_version") != "2.0.0":
        raise DomainError("invalid_contract", "患者工具名称或契约版本不匹配。")
    if body.get("status") not in {"success", "partial", "available", "unavailable", "failed"}:
        raise DomainError("invalid_contract", "患者工具返回了未知状态。")
    query = body.get("query_scope")
    if (
        not isinstance(query, dict)
        or query.get("effective") != scope.data_scope
        or not isinstance(query.get("scope_fingerprint"), str)
        or not query["scope_fingerprint"]
    ):
        raise DomainError("patient_scope_mismatch", "患者工具未确认当前查询范围。")
    if not binding and (
        not scope.has_patient or query["scope_fingerprint"] != scope.scope_fingerprint
    ):
        raise DomainError(
            "patient_scope_changed", "患者授权范围已变化，请重新确认身份。", outcome="unavailable"
        )
    data = body.get("data")
    if binding and (
        not isinstance(data, dict)
        or not isinstance(data.get("patient_ref"), str)
        or not data["patient_ref"].startswith("patient_scope_v2_")
    ):
        raise DomainError("invalid_contract", "患者工具未返回有效的患者引用。")
    if isinstance(data, dict):
        refs = [data.get("patient_ref"), (data.get("session") or {}).get("patient_ref")]
        if not binding and any(ref is not None and ref != scope.patient_ref for ref in refs):
            raise DomainError("patient_scope_mismatch", "工具返回了其他患者的信息，已停止使用。")


LEGACY_PATIENT_KEYS = frozenset(
    {
        "patientId",
        "patient_id",
        "robotDbUserId",
        "projectPatientId",
        "project_patient_id",
        "iretourPatientId",
        "iretour_patient_id",
        "patientPhone",
        "patientphone",
        "patient_phone",
    }
)


def space_id_from_inputs(inputs: dict[str, Any]) -> str | None:
    """Normalize existing gateway aliases without silently selecting another space."""
    values = set()
    for key in ("space_id", "spaceId", "spaceid"):
        value = inputs.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError("空间标识必须是字符串")
        if value.strip():
            values.add(value.strip())
    if len(values) > 1:
        raise ValueError("spaceid、spaceId、space_id 必须指向同一空间")
    return next(iter(values), None)


def requester_phone_from_inputs(inputs: dict[str, Any]) -> str | None:
    """从请求 inputs 中取 requesterphone（兼容常见别名），仅用于免签推断。"""
    for key in ("requesterphone", "requesterPhone", "requester_phone"):
        value = inputs.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if 0 < len(cleaned) <= 64:
                return cleaned
    return None


def trusted_scope_from_inputs(
    inputs: dict[str, Any],
    end_user_id: str,
    default_tenant_id: str,
    *,
    require_identity_token: bool = True,
    infer_from_requesterphone: bool = False,
) -> TrustedScope:
    if LEGACY_PATIENT_KEYS.intersection(inputs):
        raise ValueError("旧患者身份格式已移除，请传 patient_identity 和 identity_context_token")
    tenant = str(inputs.get("tenant_id", inputs.get("tenantId", default_tenant_id))).strip()
    # 合并前 main 口径：identity（患者/医生）、role 均可推断角色，缺省按患者。
    role = inputs.get(
        "role",
        {
            "医生": "clinician",
            "doctor": "clinician",
            "clinician": "clinician",
            "患者": "patient",
        }.get(inputs.get("identity"), "patient"),
    )
    if not tenant or not end_user_id.strip() or role not in {"patient", "clinician", "operator"}:
        raise ValueError("可信用户、租户或角色无效")
    identity = inputs.get("patient_identity")
    token = inputs.get("identity_context_token", "")
    if token is None and not require_identity_token:
        token = ""
    if token and (not isinstance(token, str) or len(token) > 8192):
        raise ValueError("登录凭证格式无效")
    if identity is not None:
        identity = PatientIdentity.model_validate(identity)
        if role != "patient" or not isinstance(token, str):
            raise ValueError("患者上下文缺少有效登录凭证")
        if require_identity_token and not token:
            raise ValueError("患者上下文缺少有效登录凭证")
    elif token:
        raise ValueError("登录凭证必须与 patient_identity 一同提供")
    # 免签推断：require_token=false 且 infer=true 且无显式身份时，患者角色从
    # requesterphone 推导 login_name（与旧部署版一致），再由 scope_for 走 WebApi 免签绑定。
    if (
        identity is None
        and not require_identity_token
        and infer_from_requesterphone
        and role == "patient"
    ):
        phone = requester_phone_from_inputs(inputs)
        if phone:
            identity = PatientIdentity(type="login_name", value=phone)
    scope = inputs.get("data_scope", "all_tenants")
    business = inputs.get("current_business_id")
    if scope not in {"all_tenants", "current_tenant"}:
        raise ValueError("data_scope 无效")
    if business is not None and (type(business) is not int or business <= 0):
        raise ValueError("current_business_id 必须为正整数")
    if identity is not None and scope == "current_tenant" and business is None:
        raise ValueError("当前机构查询需要 current_business_id")
    version = inputs.get("scene_version", inputs.get("sceneVersion"))
    if version is not None and (isinstance(version, bool) or not str(version).isdigit()):
        raise ValueError("scene_version 必须是非负整数")
    space = space_id_from_inputs(inputs)
    consultation = inputs.get("consultation_id", "")
    if not isinstance(consultation, str) or len(consultation) > 256:
        raise ValueError("consultation_id 无效")
    return TrustedScope(
        tenant,
        end_user_id.strip(),
        identity,
        role,
        space,
        int(version) if version is not None else None,
        business,
        scope,
        token,
        allow_unsigned_patient_identity=not require_identity_token,
        position=str(inputs.get("pos") or "").strip(),
        consultation_id=consultation,
    )
