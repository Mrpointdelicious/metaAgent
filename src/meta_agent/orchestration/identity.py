"""
创建日期：2026-08-29
文件功能：规范化可信身份，生成多租户、多患者和多会话隔离键。
"""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any


def _unwrap_mixed_value(value: Any) -> Any:
    """兼容 Dify mixed 参数误传为 {type, value} 的历史形式。"""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _first_value(inputs: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in inputs and inputs[name] not in (None, ""):
            return _unwrap_mixed_value(inputs[name])
    return None


@dataclass(frozen=True, slots=True)
class TrustedScope:
    """一次运行已经通过可信服务认证的作用域。"""

    tenant_id: str
    end_user_id: str
    patient_id: str | None = None
    role: str = "patient"
    space_id: str | None = None
    scene_version: int | None = None

    @property
    def scope_hash(self) -> str:
        material = json.dumps(
            [self.tenant_id, self.end_user_id, self.role, self.patient_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(material.encode("utf-8")).hexdigest()

    def thread_id(self, conversation_id: str) -> str:
        material = json.dumps([self.scope_hash, conversation_id], separators=(",", ":"))
        return sha256(material.encode("utf-8")).hexdigest()


def trusted_scope_from_inputs(
    inputs: dict[str, Any],
    end_user_id: str,
    default_tenant_id: str,
) -> TrustedScope:
    """仅供已认证可信网关调用；患者对普通问答、医生和场景任务可选。"""
    raw_patient = _first_value(inputs, ("patientId", "patient_id", "robotDbUserId"))
    patient_id = None if raw_patient is None else str(raw_patient).strip()
    if patient_id is not None and (
        not patient_id.isascii() or not patient_id.isdigit() or int(patient_id) <= 0
    ):
        raise ValueError("patient_id 必须是可信患者端注入的正整数用户ID")
    if patient_id is not None:
        patient_id = str(int(patient_id))
    raw_tenant = _first_value(inputs, ("tenantId", "tenant_id"))
    tenant_id = str(raw_tenant or default_tenant_id).strip()
    if not tenant_id:
        raise ValueError("tenant_id 不能为空")
    role = str(inputs.get("role") or "patient")
    if role not in {"patient", "clinician", "operator"}:
        raise ValueError("role无效")
    space = _first_value(inputs, ("space_id", "spaceId"))
    version = _first_value(inputs, ("scene_version", "sceneVersion"))
    if version is not None and (isinstance(version, bool) or not str(version).isdigit()):
        raise ValueError("scene_version必须是非负整数")
    if not end_user_id.strip():
        raise ValueError("可信用户标识不能为空")
    return TrustedScope(
        tenant_id=tenant_id,
        end_user_id=end_user_id.strip(),
        patient_id=patient_id,
        role=role,
        space_id=str(space).strip() if space is not None else None,
        scene_version=int(version) if version is not None else None,
    )
