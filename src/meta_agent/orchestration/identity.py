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

    @property
    def has_patient(self) -> bool:
        return bool(
            self.patient_identity
            and self.identity_context_token
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
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(material.encode()).hexdigest()

    def thread_id(self, conversation_id: str) -> str:
        return sha256(json.dumps([self.scope_hash, conversation_id]).encode()).hexdigest()

    def patient_payload(self, arguments: dict, conversation_id: str = "") -> dict:
        if not self.patient_identity or not self.identity_context_token or self.role != "patient":
            raise DomainError(
                "patient_required", "请通过登录入口确认患者身份。", outcome="unavailable"
            )
        return {
            **arguments,
            "system_context": {
                "user": self.end_user_id,
                "conversation_id": conversation_id,
                "patient_identity": self.patient_identity.model_dump(),
                "current_business_id": self.current_business_id,
                "identity_context_token": self.identity_context_token,
            },
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


def trusted_scope_from_inputs(
    inputs: dict[str, Any], end_user_id: str, default_tenant_id: str
) -> TrustedScope:
    if LEGACY_PATIENT_KEYS.intersection(inputs):
        raise ValueError("旧患者身份格式已移除，请传 patient_identity 和 identity_context_token")
    tenant = str(inputs.get("tenant_id", inputs.get("tenantId", default_tenant_id))).strip()
    role = inputs.get("role", "patient")
    if not tenant or not end_user_id.strip() or role not in {"patient", "clinician", "operator"}:
        raise ValueError("可信用户、租户或角色无效")
    identity = inputs.get("patient_identity")
    token = inputs.get("identity_context_token", "")
    if identity is not None:
        identity = PatientIdentity.model_validate(identity)
        if not isinstance(token, str) or not token or len(token) > 8192 or role != "patient":
            raise ValueError("患者上下文缺少有效登录凭证")
    elif token:
        raise ValueError("登录凭证必须与 patient_identity 一同提供")
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
    space = inputs.get("space_id", inputs.get("spaceId"))
    return TrustedScope(
        tenant,
        end_user_id.strip(),
        identity,
        role,
        str(space).strip() if space is not None else None,
        int(version) if version is not None else None,
        business,
        scope,
        token,
    )
