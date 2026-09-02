"""
创建日期：2026-08-29
文件功能：规范化可信身份，生成多租户、多患者和多会话隔离键。
"""

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
    patient_id: str

    @property
    def scope_hash(self) -> str:
        material = f"{self.tenant_id}|{self.end_user_id}|{self.patient_id}"
        return sha256(material.encode("utf-8")).hexdigest()

    def thread_id(self, conversation_id: str) -> str:
        material = f"{self.scope_hash}|{conversation_id}"
        return sha256(material.encode("utf-8")).hexdigest()


def trusted_scope_from_inputs(
    inputs: dict[str, Any],
    end_user_id: str,
    default_tenant_id: str,
) -> TrustedScope:
    """从已认证 Dify 服务输入中提取患者范围，拒绝无效患者ID。"""
    raw_patient = _first_value(inputs, ("patientId", "patient_id", "robotDbUserId"))
    patient_id = "" if raw_patient is None else str(raw_patient).strip()
    if not patient_id.isdigit() or int(patient_id) <= 0:
        raise ValueError("patient_id 必须是可信患者端注入的正整数用户ID")
    raw_tenant = _first_value(inputs, ("tenantId", "tenant_id"))
    tenant_id = str(raw_tenant or default_tenant_id).strip()
    if not tenant_id:
        raise ValueError("tenant_id 不能为空")
    return TrustedScope(
        tenant_id=tenant_id,
        end_user_id=end_user_id.strip(),
        patient_id=patient_id,
    )
