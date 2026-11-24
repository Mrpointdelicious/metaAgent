"""
创建日期：2026-11-23
文件功能：通过受信业务授权方核验当前就诊或机构，不接受客户端自报的授权。
"""

import time
from dataclasses import replace
from typing import Literal

import httpx
from pydantic import ConfigDict, Field, SecretStr, model_validator

from meta_agent.contracts import DomainError, StrictModel, fingerprint
from meta_agent.roles import ROLE_AGENTS

HospitalSection = Literal[
    "all", "overview", "consultation", "rooms", "assessment_training", "records", "analysis"
]


class ResourceGrant(StrictModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    user: str
    role: Literal["clinician", "patient", "operator"]
    client_id: str
    expires_at: int = Field(gt=0, strict=True)
    capabilities: frozenset[str] = Field(min_length=1)
    consultation_id: str = ""
    patient_ref: str = ""
    patient_phone: SecretStr = SecretStr("")
    requester_phone: SecretStr = SecretStr("")
    encounter_summary: SecretStr = SecretStr("")
    hospital_id: int | None = Field(default=None, gt=0, strict=True)
    sections: frozenset[HospitalSection] = frozenset()

    @model_validator(mode="after")
    def validate_resource(self):
        clinical = self.role != "operator"
        allowed = ROLE_AGENTS.for_role(self.role).capabilities
        if not self.capabilities <= {
            name for name in allowed if name.startswith("clinical." if clinical else "hospital.")
        }:
            raise ValueError("授权能力不属于当前角色")
        if clinical:
            if not all((self.consultation_id, self.patient_ref, *self.lookup_inputs().values())):
                raise ValueError("就诊授权缺少业务绑定")
            if self.hospital_id is not None or self.sections:
                raise ValueError("就诊授权不能包含运营范围")
        elif (
            self.hospital_id is None
            or self.consultation_id
            or self.patient_ref
            or any(self.lookup_inputs().values())
            or self.encounter_summary
            or ("hospital.metrics" in self.capabilities and not self.sections)
        ):
            raise ValueError("机构授权范围无效")
        return self

    def lookup_inputs(self):
        return {
            "patientphone": self.patient_phone.get_secret_value(),
            "requesterphone": self.requester_phone.get_secret_value(),
        }

    def valid_for(self, scope):
        return (
            self.expires_at > time.time()
            and (self.tenant_id, self.user, self.role, self.client_id)
            == (scope.tenant_id, scope.end_user_id, scope.role, scope.client_id)
            and (
                self.hospital_id == scope.current_business_id
                if self.role == "operator"
                else self.consultation_id == scope.consultation_id
                and (
                    self.role != "patient"
                    or scope.has_patient
                    and self.patient_ref == scope.patient_ref
                )
            )
        )

    def scope_key(self):
        return fingerprint(
            [
                "role-resource-v1",
                self.consultation_id,
                self.patient_ref,
                self.lookup_inputs(),
                self.hospital_id,
                sorted(self.sections),
                sorted(self.capabilities),
            ]
        )


async def verify_resource(scope, token, settings, *, transport=None):
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise ValueError("resource_context_token 无效")
    if not (
        scope.role == "operator"
        and scope.current_business_id
        or scope.role in {"clinician", "patient"}
        and scope.consultation_id
    ):
        raise ValueError("缺少当前角色对应的就诊或机构标识")
    if not settings.resource_authorization_url or not settings.resource_authorization_token:
        raise DomainError(
            "resource_verifier_unavailable", "当前未接入业务授权校验。", outcome="unavailable"
        )
    payload = {
        "user": scope.end_user_id,
        "tenant_id": scope.tenant_id,
        "role": scope.role,
        "client_id": scope.client_id,
        "resource_context_token": token,
        "consultation_id": scope.consultation_id,
        "hospital_id": scope.current_business_id,
    }
    try:
        async with (
            httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False, transport=transport
            ) as client,
            client.stream(
                "POST",
                str(settings.resource_authorization_url),
                json=payload,
                headers={
                    "Authorization": "Bearer "
                    + settings.resource_authorization_token.get_secret_value()
                },
            ) as response,
        ):
            if response.status_code in (401, 403, 404):
                raise DomainError(
                    "resource_denied", "没有当前业务资源的访问授权。", outcome="unsupported"
                )
            response.raise_for_status()
            raw = bytearray()
            async for part in response.aiter_bytes():
                raw.extend(part)
                if len(raw) > 16384:
                    raise ValueError("oversized authorization response")
        grant = ResourceGrant.model_validate_json(bytes(raw))
    except (httpx.HTTPError, ValueError):
        raise DomainError(
            "resource_verifier_unavailable", "业务授权校验暂不可用。", outcome="unavailable"
        ) from None
    if not grant.valid_for(scope):
        raise DomainError(
            "resource_denied", "授权已过期或不属于本次访问范围。", outcome="unsupported"
        )
    return replace(scope, resource_grant=grant)
