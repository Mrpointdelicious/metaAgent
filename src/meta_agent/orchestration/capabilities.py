"""
创建日期：2026-11-23
文件功能：为模型、旧规划器及直接业务处理器执行同一套角色和资源授权规则。
"""

import logging
from dataclasses import replace

from meta_agent.contracts import DomainError, fingerprint
from meta_agent.integrations.knowledge import sources_for_scope
from meta_agent.medical_tools.registry import (
    ASSESSMENT_REPORT_ENDPOINTS,
    IRECOG_REPORT_ENDPOINTS,
    IRETOUR_REPORT_ENDPOINTS,
    PATIENT_ENDPOINTS,
)
from meta_agent.memory.schemas import TOOL_NAME as MEMORY_TOOL
from meta_agent.roles import FIXED_CLINICAL_CAPABILITIES, ROLE_AGENTS

logger = logging.getLogger(__name__)

CLINICAL_TASKS = {
    "基础问答": "clinical.basic",
    "指令功能": "clinical.basic",
    "病历填写": "clinical.record",
    "医嘱填写": "clinical.order",
    "处方配置": "clinical.prescription",
    "量表测评": "clinical.scale",
    "邀请医生": "clinical.invite",
    "结束问诊": "clinical.end",
    "纪要总结": "clinical.summary",
    "评估分析": "clinical.analysis",
}

OPERATIONS = {
    **dict.fromkeys(PATIENT_ENDPOINTS, "patient.read"),
    **dict.fromkeys(
        (
            "irego.execute",
            "iretour.execute",
            "irecog.execute",
            "assessment.execute",
            "rehab.overview",
            "rehab.history",
            "rehab.resolve_session",
            "rehab.session",
            "rehab.single_report",
            "rehab.trend",
            "rehab.trend_report",
        ),
        "patient.read",
    ),
    "resolve_patient_identity": "patient.bind",
    MEMORY_TOOL: "patient.preference",
    "search_doctors": "doctors.search",
    "open_chat_history": "history.open",
    "resolve_scene_instruction": "scene.preview",
    "scene.resolve": "scene.preview",
    "navigate_scene": "scene.act",
    "scene.dispatch": "scene.act",
    "query_hospital_directory": "hospital.directory",
    "query_hospital_operations": "hospital.metrics",
    "hospital.query": "hospital.metrics",
    "search_knowledge": "knowledge.search",
}

SWITCHES = {
    **dict.fromkeys(IRETOUR_REPORT_ENDPOINTS, "iretour_reports_enabled"),
    **dict.fromkeys(IRECOG_REPORT_ENDPOINTS, "irecog_reports_enabled"),
    **dict.fromkeys(ASSESSMENT_REPORT_ENDPOINTS, "assessment_reports_enabled"),
    "get_multisource_patient_context": "multisource_patient_context_enabled",
}


def bind_access_scope(scope, settings, excluded=()):
    ROLE_AGENTS.for_role(scope.role)
    if (scope.role == "operator") != (scope.client_id == "backoffice"):
        raise DomainError("role_access_denied", "该接入方无权使用所选角色。", outcome="unsupported")
    if scope.resource_grant and not scope.resource_grant.valid_for(scope):
        raise DomainError("resource_denied", "当前资源授权已失效。", outcome="unsupported")
    bounds = []
    if scope.role == "operator":
        bounds.append(["backoffice", scope.current_business_id])
    if settings.knowledge_sources or settings.knowledge_corpus_path or settings.portfolio_enabled:
        bounds.append(
            [
                "knowledge-acl-v1",
                settings.knowledge_access_revision,
                scope.space_id,
                [
                    [
                        source.id,
                        source.domains,
                        source.scoped_domains,
                        str(source.url),
                        source.maxkb.catalog_sha256 if source.maxkb else None,
                        sorted(source.maxkb.allowed_space_ids) if source.maxkb else [],
                    ]
                    for source in sources_for_scope(settings.knowledge_sources, scope)
                ],
                settings.knowledge_corpus_path,
                settings.portfolio_kb_ids,
                settings.portfolio_space_kb_ids,
                sorted(settings.portfolio_roles),
            ]
        )
    if scope.role in {"patient", "clinician"} and not settings.hospital_ops_open_to_all_roles:
        bounds.append(["restricted-hospital", settings.hospital_directory_public])
    if scope.client_flow:
        bounds.append(["client_flow", scope.client_flow])
    if scope.allowed_tools is not None:
        bounds.append(["allowed_tools", sorted(scope.allowed_tools)])
    if excluded:
        bounds.append(sorted(excluded))
    return replace(scope, policy_scope=fingerprint(bounds) if bounds else "")


def _client_permits(operation, capability, allowed, excluded):
    if operation in excluded or capability in excluded:
        return False
    if capability.startswith("clinical.") or capability in {"patient.bind", "answer.compose"}:
        return True
    if "." not in operation:
        return allowed is None or operation in allowed
    # Internal adapters translate a model tool to a domain operation. The gateway
    # checks the concrete endpoint again before the underlying request.
    if operation == "scene.resolve":
        names = {"resolve_scene_instruction", "navigate_scene"}
    else:
        names = {name for name, cap in OPERATIONS.items() if cap == capability and "." not in name}
    return bool((names if allowed is None else names & allowed).difference(excluded))


def denial(scope, settings, operation, *, excluded=(), arguments=None):
    # 回退开关：跳过整套 dev 角色能力校验，回到合并前 main 的不拦截行为。
    if getattr(settings, "disable_capability_policy", False):
        return None
    capability = OPERATIONS.get(operation, operation)
    profile = ROLE_AGENTS.for_role(scope.role)
    if scope.role in getattr(settings, "disabled_agent_roles", ()):
        return "agent_unavailable"
    if (scope.role == "operator") != (getattr(scope, "client_id", None) == "backoffice"):
        return "role_denied"
    if capability not in profile.capabilities:
        return "role_denied"
    if not _client_permits(operation, capability, getattr(scope, "allowed_tools", None), excluded):
        return "client_denied"
    switch = SWITCHES.get(operation)
    if switch and not getattr(settings, switch):
        return "disabled"
    if capability == "patient.bind" and not (
        scope.patient_identity and scope.identity_context_token
    ):
        return "patient_required"
    if capability in {"patient.read", "patient.preference"} and not scope.has_patient:
        return "patient_required"
    if capability == "patient.preference" and not settings.patient_memory_enabled:
        return "disabled"
    if capability == "doctors.search" and not settings.doctors_enabled:
        return "disabled"
    if capability in {"scene.preview", "scene.act"} and not settings.scene_enabled:
        return "disabled"
    if capability == "scene.act" and not settings.scene_actions_enabled:
        return "disabled"
    if capability == "knowledge.search":
        sources = getattr(settings, "knowledge_sources", ())
        if (sources or profile.role == "student") and not sources_for_scope(sources, scope):
            return "knowledge_scope_required"
    if capability.startswith("clinical."):
        branch = getattr(scope, "agent_kind", None)
        if branch not in {"consultation_room", "ai_consultation"}:
            fixed = FIXED_CLINICAL_CAPABILITIES.get(branch)
            if capability != fixed and not (
                branch == "ai_prescription" and capability == "clinical.prescription.create"
            ):
                return "consultation_required"
        # 合并前 main 不按 pos 限制临床能力；功能令牌在大厅也可走对应流程。
    if capability in {"hospital.directory", "hospital.metrics"} and scope.role != "operator":
        if not (
            capability == "hospital.directory"
            and getattr(settings, "hospital_directory_public", False)
            or getattr(settings, "hospital_ops_open_to_all_roles", False)
        ):
            return "operator_required"
    elif (
        capability.startswith("hospital.")
        or capability.startswith("clinical.")
        and capability != "clinical.basic"
    ):
        grant = getattr(scope, "resource_grant", None)
        if not grant or not grant.valid_for(scope) or capability not in grant.capabilities:
            return "resource_denied"
        if (
            capability == "hospital.metrics"
            and arguments is not None
            and (
                arguments.get("scope", "institution") != "institution"
                or arguments.get("hospital_ids")
                or arguments.get("hospital_id") not in (None, grant.hospital_id)
                or (
                    "all" not in grant.sections
                    and arguments.get("section", "all") not in grant.sections
                )
                or (
                    "all" not in grant.sections
                    and arguments.get("output_mode", "analysis") != "analysis"
                )
            )
        ):
            return "resource_denied"
    return None


def require(scope, settings, operation, *, excluded=(), arguments=None):
    reason = denial(scope, settings, operation, excluded=excluded, arguments=arguments)
    profile = ROLE_AGENTS.for_role(scope.role)
    logger.info(
        "role capability decision",
        extra={
            "event_type": "role_policy",
            "fields": {
                "role": scope.role,
                "agent_id": profile.agent_id,
                "profile_version": profile.version,
                "flow": getattr(scope, "agent_kind", ""),
                "capability": OPERATIONS.get(operation, operation),
                "decision": reason or "allowed",
            },
        },
    )
    if reason:
        raise DomainError(reason, "当前角色或授权范围不允许此操作。", outcome="unsupported")


def require_for_run(ctx, operation, arguments=None):
    client = getattr(ctx, "client", None)
    require(
        ctx.scope,
        ctx.settings,
        operation,
        excluded=client.excluded_tools if client else (),
        arguments=arguments,
    )
