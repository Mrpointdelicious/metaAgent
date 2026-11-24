"""
创建日期：2026-10-29
文件功能：提供隔离测试的合成患者身份，不签发真实业务凭证。
"""

import time
from dataclasses import replace

from pydantic import SecretStr

from meta_agent.medical_tools.contracts import ToolContext
from meta_agent.orchestration.capabilities import bind_access_scope
from meta_agent.orchestration.identity import PatientIdentity, TrustedScope
from meta_agent.orchestration.resources import ResourceGrant
from meta_agent.tools.demo import synthetic_binding
from tests.helpers.settings import AppTestSettings


def patient_scope(
    tenant_id,
    end_user_id,
    patient_id="461",
    *,
    role="patient",
    space_id=None,
    scene_version=None,
    data_scope="all_tenants",
    current_business_id=None,
    identity_type="project_user_id",
):
    identity = PatientIdentity(type=identity_type, value=str(patient_id))
    ref, digest = synthetic_binding(
        {
            "system_context": {
                "user": end_user_id,
                "patient_identity": identity.model_dump(),
                "current_business_id": current_business_id,
            },
            "data_scope": data_scope,
        }
    )
    scope = TrustedScope(
        tenant_id,
        end_user_id,
        identity,
        role,
        space_id,
        scene_version,
        current_business_id,
        data_scope,
        "synthetic-proof-only",
        ref,
        digest,
    )
    return bind_access_scope(scope, AppTestSettings())


def patient_inputs(value="461", identity_type="project_user_id", **extra):
    return {
        "role": "patient",
        "patient_identity": {"type": identity_type, "value": str(value)},
        "identity_context_token": "synthetic-proof-only",
        **extra,
    }


def clinical_scope(
    visit="visit-a", patient="target-a", *, scope=None, phone="13000000002", capabilities=None
):
    scope = replace(
        scope or TrustedScope("tenant", "doctor", role="clinician"), consultation_id=visit
    )
    grant = ResourceGrant(
        tenant_id=scope.tenant_id,
        user=scope.end_user_id,
        role=scope.role,
        client_id=scope.client_id,
        consultation_id=visit,
        patient_ref=scope.patient_ref or patient,
        patient_phone=phone,
        requester_phone="13000000001",
        encounter_summary="仅限本次就诊的合成摘要",
        capabilities=capabilities
        or {
            "clinical.record",
            "clinical.order",
            "clinical.prescription",
            "clinical.scale",
            "clinical.invite",
            "clinical.end",
        },
        expires_at=int(time.time()) + 300,
    )
    return replace(scope, resource_grant=grant)


def clinical_flow_scope(branch, *, summary="", client_id="primary"):
    scope = clinical_scope(
        scope=TrustedScope("tenant", "doctor", role="clinician", client_id=client_id),
        capabilities={
            "clinical.summary",
            "clinical.order",
            "clinical.analysis",
            "clinical.prescription",
        },
    )
    grant = scope.resource_grant.model_copy(update={"encounter_summary": SecretStr(summary)})
    return replace(scope, agent_kind=branch, resource_grant=grant)


def operator_scope(hospital=1, sections=("all",)):
    scope = TrustedScope(
        "tenant", "operator", role="operator", client_id="backoffice", current_business_id=hospital
    )
    grant = ResourceGrant(
        tenant_id=scope.tenant_id,
        user=scope.end_user_id,
        role="operator",
        client_id="backoffice",
        hospital_id=hospital,
        capabilities={"hospital.directory", "hospital.metrics"},
        sections=sections,
        expires_at=int(time.time()) + 300,
    )
    return replace(scope, resource_grant=grant)


def public_tool_context(space_id=None):
    return ToolContext(TrustedScope("tenant", "actor", space_id=space_id))
