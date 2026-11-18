"""
创建日期：2026-11-17
文件功能：集中登记医疗工具名称、参数、功能开关及提供方路由。

本模块是工具集合的**唯一来源**：`tools/ai_webapi.py` 与 `application/context.py`
都从这里取端点集合，避免三处各写一份导致漂移。
"""

from dataclasses import dataclass
from typing import Literal

from meta_agent.config import Settings
from meta_agent.contracts import HospitalRequest
from meta_agent.medical_tools.contracts import (
    DoctorArguments,
    PatientArguments,
    SceneArguments,
)
from meta_agent.medical_tools.errors import ToolError

PATIENT_ENDPOINTS = frozenset(
    {
        "get_multisource_patient_context",
        "resolve_patient_identity",
        "get_patient_profile",
        "get_patient_consultation",
        "get_patient_rehab",
        "get_irego_session_analysis",
        "get_irego_patient_history",
        "get_irego_longitudinal_analysis",
        "generate_irego_single_session_report",
        "generate_irego_longitudinal_report",
        "get_iretour_patient_context",
        "get_iretour_patient_history",
        "get_iretour_session_analysis",
        "get_iretour_longitudinal_analysis",
        "generate_iretour_single_session_report",
        "generate_iretour_longitudinal_report",
        "get_irecog_patient_context",
        "get_irecog_patient_history",
        "get_irecog_session_analysis",
        "get_irecog_longitudinal_analysis",
        "generate_irecog_single_session_report",
        "generate_irecog_longitudinal_report",
        "get_assessment_patient_context",
        "get_assessment_patient_history",
        "get_assessment_result_analysis",
        "get_assessment_longitudinal_analysis",
        "generate_assessment_single_report",
        "generate_assessment_longitudinal_report",
    }
)
REHAB_ENDPOINTS = frozenset({"navigate_scene", "search_doctors"})
HOSPITAL_ENDPOINTS = frozenset({"query_hospital_operations"})
# 三层患者接口使用 project 患者编号，独立于已停用的多源端点。
PATIENT_ENDPOINTS_V17 = frozenset(
    {"get_patient_profile", "get_patient_consultation", "get_patient_rehab"}
)
IRETOUR_REPORT_ENDPOINTS = frozenset(
    {"generate_iretour_single_session_report", "generate_iretour_longitudinal_report"}
)
IRECOG_REPORT_ENDPOINTS = frozenset(
    {"generate_irecog_single_session_report", "generate_irecog_longitudinal_report"}
)
ASSESSMENT_REPORT_ENDPOINTS = frozenset(
    {"generate_assessment_single_report", "generate_assessment_longitudinal_report"}
)
# 机构目录是 hospital-operations 域的独立 GET 接口，不经过 /tools 的 POST。
DIRECTORY_ENDPOINT = "query_hospital_directory"

Provider = Literal["ai_webapi", "platform_scene", "platform_doctors"]

# 功能开关关闭时的对外口径（与 ai_webapi 原有文案一致，不暴露内部开关名）。
_DISABLED_MESSAGES = {
    "multisource_patient_context_enabled": "多源患者上下文暂未启用。",
    "iretour_reports_enabled": "IReTour报表暂未启用，仍可查询历史和分析结果。",
    "irecog_reports_enabled": "IReCog报表暂未启用，仍可查询历史和分析结果。",
    "assessment_reports_enabled": "量表报表暂未启用，仍可查询历史和分析结果。",
}


@dataclass(frozen=True)
class ToolSpec:
    arguments: type
    provider: Provider = "ai_webapi"
    enabled_setting: str | None = None


def registry(settings: Settings) -> dict[str, ToolSpec]:
    specs = {name: ToolSpec(PatientArguments) for name in PATIENT_ENDPOINTS}
    for endpoints, setting in (
        (IRETOUR_REPORT_ENDPOINTS, "iretour_reports_enabled"),
        (IRECOG_REPORT_ENDPOINTS, "irecog_reports_enabled"),
        (ASSESSMENT_REPORT_ENDPOINTS, "assessment_reports_enabled"),
        ({"get_multisource_patient_context"}, "multisource_patient_context_enabled"),
    ):
        specs.update(
            {name: ToolSpec(PatientArguments, enabled_setting=setting) for name in endpoints}
        )
    specs.update(
        {
            "navigate_scene": ToolSpec(
                SceneArguments, "platform_scene" if settings.scene_local_enabled else "ai_webapi"
            ),
            "search_doctors": ToolSpec(
                DoctorArguments,
                "platform_doctors" if settings.doctors_local_enabled else "ai_webapi",
            ),
            DIRECTORY_ENDPOINT: ToolSpec(dict),
            "query_hospital_operations": ToolSpec(HospitalRequest),
        }
    )
    return specs


def require_enabled(name: str, settings: Settings) -> ToolSpec:
    """取工具规格；未注册或功能开关关闭时抛 ToolError（与原有状态/文案一致）。"""
    spec = registry(settings).get(name)
    if spec is None:
        raise ToolError("unknown_tool", "未注册的工具端点。")
    if spec.enabled_setting and not getattr(settings, spec.enabled_setting):
        raise ToolError(
            "capability_disabled",
            _DISABLED_MESSAGES[spec.enabled_setting],
            outcome="unsupported",
        )
    return spec
