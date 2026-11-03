"""
创建日期：2026-10-29
文件功能：提供隔离测试的合成患者身份，不签发真实业务凭证。
"""

from meta_agent.orchestration.identity import PatientIdentity, TrustedScope
from meta_agent.tools.demo import synthetic_binding


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
    return TrustedScope(
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


def patient_inputs(value="461", identity_type="project_user_id", **extra):
    return {
        "patient_identity": {"type": identity_type, "value": str(value)},
        "identity_context_token": "synthetic-proof-only",
        **extra,
    }
