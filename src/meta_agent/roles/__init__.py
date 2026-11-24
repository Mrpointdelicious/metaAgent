"""
创建日期：2026-11-23
文件功能：注册活动角色对应的 Agent 方案、场景和能力上限。
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, get_args

from meta_agent.roles.prompts import (
    CLINICIAN_SYSTEM_PROMPT,
    OPERATOR_SYSTEM_PROMPT,
    STUDENT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)

Role = Literal["patient", "clinician", "student", "operator"]
USER_ROLES = get_args(Role)


@dataclass(frozen=True, slots=True)
class RoleAgentProfile:
    role: Role
    agent_id: str
    prompt: str
    flows: frozenset[str]
    capabilities: frozenset[str]
    memory: Literal["patient_preferences", "session"]
    fallback: Literal["legacy", "institution_query", "unavailable"]
    unavailable: str
    version: str = "1"


class RoleAgentRegistry:
    def __init__(self, *profiles: RoleAgentProfile):
        by_role = {profile.role: profile for profile in profiles}
        if set(by_role) != set(USER_ROLES) or len(profiles) != len(by_role):
            raise ValueError("每个活动角色必须且只能注册一份方案")
        if len({profile.agent_id for profile in profiles}) != len(profiles):
            raise ValueError("agent_id 必须唯一")
        self.profiles = MappingProxyType(by_role)

    def for_role(self, role: str) -> RoleAgentProfile:
        if role not in self.profiles:
            raise ValueError("可信用户角色无效")
        return self.profiles[role]


DEFAULT = frozenset({"default"})
WITH_ROOM = DEFAULT | {"consultation_room", "ai_consultation"}
FIXED_CLINICAL_CAPABILITIES = MappingProxyType(
    {
        "ai_summary": "clinical.summary",
        "ai_medical_order": "clinical.order",
        "ai_prescription": "clinical.prescription",
        "ai_realtime_analysis": "clinical.analysis",
        # 完整 AI 问诊链路（11-18 从 main 补入）：问诊分支复用 clinical.basic，
        # 该能力在 clinician/patient 角色集合内且豁免 resource_grant 校验。
        "ai_consultation": "clinical.basic",
    }
)
COMMON = frozenset({"knowledge.search", "answer.compose"})
HOSPITAL = frozenset({"hospital.directory", "hospital.metrics"})
PARTICIPANT = frozenset({"clinical.basic", "clinical.invite", "clinical.end"})
FRONT_DESK = frozenset({"history.open", "scene.preview", "scene.act"})
CLINICAL = (
    PARTICIPANT
    | {
        "clinical.record",
        "clinical.order",
        "clinical.prescription",
        "clinical.scale",
        "clinical.prescription.create",
    }
    | frozenset(FIXED_CLINICAL_CAPABILITIES.values())
)

ROLE_AGENTS = RoleAgentRegistry(
    RoleAgentProfile(
        "patient",
        "patient_rehab",
        SYSTEM_PROMPT,
        WITH_ROOM,
        COMMON
        | HOSPITAL
        | FRONT_DESK
        | PARTICIPANT
        | {"patient.bind", "patient.read", "patient.preference", "doctors.search"},
        "patient_preferences",
        "legacy",
        "当前患者康复助手暂不可用。",
    ),
    RoleAgentProfile(
        "clinician",
        "clinician_workspace",
        CLINICIAN_SYSTEM_PROMPT,
        WITH_ROOM | frozenset(FIXED_CLINICAL_CAPABILITIES),
        COMMON | HOSPITAL | FRONT_DESK | CLINICAL,
        "session",
        "unavailable",
        "当前医生问诊助手暂不可用。",
    ),
    RoleAgentProfile(
        "student",
        "student_tutor",
        STUDENT_SYSTEM_PROMPT,
        DEFAULT,
        COMMON,
        "session",
        "unavailable",
        "已识别为学生，当前环境未启用学习助手，暂时不能提供学习辅导。",
    ),
    RoleAgentProfile(
        "operator",
        "hospital_ops",
        OPERATOR_SYSTEM_PROMPT,
        DEFAULT,
        COMMON | HOSPITAL,
        "session",
        "institution_query",
        "当前机构分析助手暂不可用。",
    ),
)
