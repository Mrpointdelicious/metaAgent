"""
创建日期：2026-10-05
文件功能：封装Agent原子工具，绑定可信身份并保存调用证据及失败结果。
"""

import asyncio
import json
import re
import time
from contextvars import ContextVar

from langchain_core.tools import StructuredTool

from meta_agent.agent.context import compact_result
from meta_agent.agent.schemas import (
    EmptyArgs,
    GoHistoryArgs,
    GoReportTrendArgs,
    GoTrendArgs,
    KnowledgeArgs,
    PatientPageArgs,
    ReportArgs,
    SceneArgs,
    SessionArgs,
    TourHistoryArgs,
    TourReportTrendArgs,
    TourTrendArgs,
)
from meta_agent.application.frontend_context import conversational_only
from meta_agent.contracts import (
    ArtifactPayload,
    DomainError,
    FailurePayload,
    Goal,
    HospitalRequest,
    RecordAnchor,
    TaskResult,
    TaskSpec,
    utcnow,
)
from meta_agent.domains.doctors import DoctorAdapter
from meta_agent.domains.facts import aware_date
from meta_agent.domains.hospital import HospitalAdapter
from meta_agent.domains.irego import IReGoWorkflow
from meta_agent.domains.knowledge import KnowledgeAdapter
from meta_agent.domains.scene import SceneAdapter, source_text
from meta_agent.events.stream import EventPersistenceError
from meta_agent.tools.ai_webapi import IRETOUR_REPORT_ENDPOINTS

CURRENT_RUN: ContextVar = ContextVar("single_agent_run")
LAYER_TOOLS = {"get_patient_profile", "get_patient_consultation", "get_patient_rehab"}

TOOL_SPECS = {
    "get_patient_profile": (
        EmptyArgs,
        "查询当前患者基本档案及自述既往史，不含就诊病历。通常已有小摘要，可按需刷新。",
    ),
    "get_patient_consultation": (
        PatientPageArgs,
        "查询当前患者就诊、病历、已记录诊断、功能障碍、医嘱及处方。可分页，最近记录在前。病史问题优先用此工具。",
    ),
    "get_patient_rehab": (
        PatientPageArgs,
        "查询当前患者康复计划及IReTour/IReGo训练历史摘要。不知道患者用过哪类设备时先查此工具。",
    ),
    "get_iretour_patient_context": (EmptyArgs, "查询IReTour训练数据覆盖范围和可用结果数量。"),
    "get_iretour_patient_history": (
        TourHistoryArgs,
        "按页查询IReTour训练历史，返回日期、项目、训练及结果状态和session_ref；支持有结果/无结果、项目和状态筛选，上一次或下一页从这里定位。",
    ),
    "get_iretour_session_analysis": (
        SessionArgs,
        "读取IReTour最近可用训练或指定session_ref的实际结果，包含指标与数据限制。",
    ),
    "get_iretour_longitudinal_analysis": (
        TourTrendArgs,
        "读取IReTour同项目的连续训练趋势，支持2至20次或日期范围，保留不可比较原因。",
    ),
    "generate_iretour_single_session_report": (
        ReportArgs,
        "仅在用户要求图片时，为已查询的IReTour session_ref生成原始报表。",
    ),
    "generate_iretour_longitudinal_report": (
        TourReportTrendArgs,
        "仅在用户要求图片时生成IReTour同项目趋势报表，至少3次。",
    ),
    "get_irego_patient_history": (
        GoHistoryArgs,
        "按页查询IReGo训练历史，返回计划、训练状态及session_ref。身份映射由系统处理。",
    ),
    "get_irego_session_analysis": (
        SessionArgs,
        "读取IReGo最近可用训练或历史返回的session_ref，不自行推断临床疗效。",
    ),
    "get_irego_longitudinal_analysis": (
        GoTrendArgs,
        "读取IReGo连续训练窗口趋势，按同一项目分段比较，2至10次。",
    ),
    "generate_irego_single_session_report": (
        ReportArgs,
        "仅在用户要求图片时，为已查询的IReGo session_ref生成原始报表。",
    ),
    "generate_irego_longitudinal_report": (
        GoReportTrendArgs,
        "仅在用户要求图片时生成IReGo连续训练趋势报表，3至10次。",
    ),
    "search_doctors": (EmptyArgs, "查询系统注入的当前空间在线医生，不联系医生。"),
    "navigate_scene": (
        SceneArgs,
        "按用户本轮原话解析并执行当前空间的场景动作；身份和目录版本由系统注入。",
    ),
    "query_hospital_operations": (HospitalRequest, "为运营人员查询医院运营数据及按需生成报表。"),
    "search_knowledge": (KnowledgeArgs, "检索已批准的康复或产品资料，可用于解释与一般建议。"),
}


def project_id(ctx):
    return ctx.scope.project_patient_id or ctx.scope.iretour_patient_id


def tool_allowed(ctx, name):
    if getattr(ctx, "frontend_context", None) and conversational_only(ctx.query):
        return False
    if name in IRETOUR_REPORT_ENDPOINTS and not ctx.settings.iretour_reports_enabled:
        return False
    if name in LAYER_TOOLS or "iretour" in name:
        return project_id(ctx) is not None
    if "irego" in name:
        return ctx.scope.patient_id is not None or project_id(ctx) is not None
    if name == "query_hospital_operations":
        return ctx.scope.role == "operator"
    if name == "navigate_scene":
        return ctx.settings.scene_enabled and ctx.settings.scene_actions_enabled
    if name == "search_doctors":
        return ctx.settings.doctors_enabled
    return name == "search_knowledge"


def available_tools(ctx, tools):
    return [tool for tool in tools if tool_allowed(ctx, tool.name)]


def build_tools():
    tools = []
    for name, (schema, description) in TOOL_SPECS.items():

        async def invoke(_name=name, **kwargs):
            return json.dumps(
                await invoke_tool(CURRENT_RUN.get(), _name, kwargs), ensure_ascii=False
            )

        tools.append(
            StructuredTool.from_function(
                name=name,
                description=description,
                coroutine=invoke,
                args_schema=schema,
                handle_validation_error=True,
            )
        )
    return tools


def spec(ctx, name, tid, capability, args, goal_ids=None):
    return TaskSpec(
        task_id=tid,
        goal_ids=goal_ids or ["agent"],
        capability=capability,
        arguments=args,
        idempotency_key=f"{ctx.record.request_id}:{tid}:{name}",
    )


async def _robot_id(ctx):
    if ctx.scope.patient_id:
        return ctx.scope.patient_id
    if ctx.agent_robot_id:
        return ctx.agent_robot_id
    pid = project_id(ctx)
    if not pid:
        raise DomainError("patient_required", "当前没有可信患者身份。", outcome="unavailable")
    async with asyncio.timeout(min(ctx.remaining, ctx.settings.tool_timeout_seconds)):
        body = await ctx.call("resolve_patient_identity", {"project_patient_id": int(pid)})
    evidence = await ctx.repository.save_evidence(
        ctx.scope.scope_hash,
        "resolve_patient_identity",
        ctx.record.request_id,
        body,
        str(body.get("contract_version", "unknown")),
    )
    data = body.get("data")
    if (
        body.get("status") != "success"
        or not isinstance(data, dict)
        or data.get("binding_status") != "resolved"
        or data.get("project_patient_id") != int(pid)
        or type(data.get("robot_patient_id")) is not int
        or data["robot_patient_id"] <= 0
    ):
        raise DomainError(
            "identity_unresolved",
            "IReGo身份映射未能唯一确认，仍可查询其他患者信息。",
            outcome="unavailable",
        )
    ctx.agent_robot_id = str(data["robot_patient_id"])
    ctx.record.metrics["robot_identity_evidence"] = evidence.evidence_id
    return ctx.agent_robot_id


async def _read(ctx, name, args):
    ref = args.get("session_ref") or ""
    if ("irego" in name and ref.startswith("iretour_s_v1_")) or (
        "iretour" in name and ref.startswith("irego_s_v1_")
    ):
        message = "记录引用属于另一类设备，请使用同一设备的查询工具。"
        if name.startswith("generate_") and not ctx.settings.iretour_reports_enabled:
            message = "IReTour报表暂未启用，不能使用IReGo报表代替；仍可查询历史和分析结果。"
        raise DomainError("record_domain_mismatch", message, outcome="unsupported")
    if name in LAYER_TOOLS or "iretour" in name:
        user = project_id(ctx)
    else:
        user = await _robot_id(ctx)
    if not user:
        raise DomainError(
            "patient_required", "当前没有可信的project患者身份。", outcome="unavailable"
        )
    payload = {
        **args,
        "system_context": {"user": user, "conversation_id": ctx.record.conversation_id},
    }
    key = "agent:profile"
    cached = (
        await ctx.repository.get("cache", ctx.scope.scope_hash, key)
        if name == "get_patient_profile"
        else None
    )
    evidence = (
        await ctx.repository.evidence(ctx.scope.scope_hash, cached["evidence_id"])
        if cached
        else None
    )
    if evidence:
        return evidence.payload, evidence, True
    timeout = (
        ctx.settings.report_timeout_seconds
        if name.startswith("generate_")
        else ctx.settings.tool_timeout_seconds
    )
    async with asyncio.timeout(min(ctx.remaining, timeout)):
        body = await ctx.call(name, payload)
    version = body.get("contract_version")
    valid_version = (
        version == "patient-layers-1.0.0"
        if name in LAYER_TOOLS
        else version == "iretour-1.0.0"
        or (
            version == "1.6.0"
            and body.get("meta", {}).get("registry_versions", {}).get("contract") == "iretour-1.0.0"
        )
        if "iretour" in name
        else version == "1.6.0"
    )
    if body.get("tool_name") != name or not valid_version:
        raise DomainError("invalid_contract", "工具名称或契约版本不匹配。")
    if body.get("status") not in {"success", "available", "partial", "unavailable", "failed"}:
        raise DomainError("invalid_contract", "工具返回了未知状态。")
    data = body.get("data")
    if (
        name in LAYER_TOOLS
        and isinstance(data, dict)
        and data.get("patient_ref") != f"project:{user}"
    ):
        raise DomainError("patient_scope_mismatch", "工具返回了其他患者的信息，已停止使用。")
    if (
        args.get("session_ref")
        and "session_analysis" in name
        and isinstance(data, dict)
        and data.get("session", {}).get("session_ref") != args["session_ref"]
    ):
        raise DomainError("record_mismatch", "工具返回了不同的训练记录。")
    evidence = await ctx.repository.save_evidence(
        ctx.scope.scope_hash, name, ctx.record.request_id, body, version
    )
    if name == "get_patient_profile" and body["status"] == "success":
        await ctx.repository.put(
            "cache",
            ctx.scope.scope_hash,
            key,
            {"evidence_id": evidence.evidence_id},
            ctx.settings.cache_ttl_seconds,
        )
    return body, evidence, False


async def _adapter(ctx, name, args, tid):
    if name == "search_doctors":
        return await DoctorAdapter().execute(
            spec(ctx, name, tid, "doctors.search", args), args, ctx
        )
    if name == "search_knowledge":
        return await KnowledgeAdapter().execute(
            spec(ctx, name, tid, "knowledge.search", args), args, ctx
        )
    if name == "query_hospital_operations":
        return await HospitalAdapter().execute(
            spec(ctx, name, tid, "hospital.query", args), args, ctx
        )
    if not ctx.settings.scene_actions_enabled or not ctx.scope.space_id:
        raise DomainError("scene_actions_disabled", "当前场景动作未启用。", outcome="unsupported")
    if source_text(args["target"]) not in source_text(ctx.query):
        raise DomainError(
            "scene_request_mismatch", "场景动作须来自本轮用户原话。", outcome="clarification"
        )
    phrases = [x.strip() for x in re.split(r"[，,；;]", args["target"]) if x.strip()]
    ids = [f"scene_{tid}_{i}" for i in range(len(phrases))]
    for gid, phrase in zip(ids, phrases, strict=True):
        ctx.goals[gid] = Goal(goal_id=gid, kind="scene_action", domain="scene", query_span=phrase)
    adapter = SceneAdapter()
    result = await adapter.execute(spec(ctx, name, tid, "scene.resolve", args, ids), args, ctx)
    ctx.record.results[tid] = result
    for gid, ref in result.outputs.get("action_ref", {}).items():
        dispatch = spec(ctx, name, tid, "scene.dispatch", {}, [gid])
        dispatch.idempotency_key += ":" + gid
        await adapter.dispatch(dispatch, ref, ctx)
    return result


async def invoke_tool(ctx, name, args):
    tid = f"agent_tool_{len(ctx.record.results) + 1}"
    started = time.monotonic()
    result = TaskResult(task_id=tid, outputs={"tool_name": name, "arguments": args})
    # Allocate before awaiting so concurrent tools get distinct audit identifiers.
    # 上下文装填记录
    ctx.record.results[tid] = result
    # 发送对应报文
    await ctx.events.progress(
        "generating_artifact" if name.startswith("generate_") else "tool", task_id=tid
    )
    # 处理超域问题
    try:
        if not tool_allowed(ctx, name):
            raise DomainError(
                "tool_not_allowed", "当前身份或配置未启用这项工具。", outcome="unsupported"
            )
        if name in {
            "search_doctors",
            "search_knowledge",
            "query_hospital_operations",
            "navigate_scene",
        }:
            # asyncio是干嘛的？
            async with asyncio.timeout(min(ctx.remaining, ctx.settings.tool_timeout_seconds)):
                # adapter又干了什么？，执行工具？跟excute区别在哪？
                adapted = await _adapter(ctx, name, args, tid)
            result = adapted
            # 构造了 Python 字典
            result.outputs["tool_name"] = name
            body = {
                "tool_name": name,
                "status": result.status,
                "patient_message": result.message,
                "facts": [
                    {
                        "label": v.label,
                        "value": v.fact.value,
                        "unit": v.fact.unit,
                        "value_status": v.fact.value_status,
                    }
                    for v in result.facts
                ],
                "data": result.outputs,
                "evidence_id": next(iter(result.evidence_ids), None),
            }
        else:
            body, evidence, cache_hit = await _read(ctx, name, args)
            result.status = {"success": "succeeded", "available": "succeeded"}.get(
                body["status"], body["status"]
            )
            # 编制对应id方便管理
            result.evidence_ids = [evidence.evidence_id]
            # 更新
            result.outputs.update(cache_hit=cache_hit)
            body = {**body, "evidence_id": evidence.evidence_id}
            data = body.get("data") or {}
            if result.status in {"succeeded", "partial"} and "session_analysis" in name:
                ref = data.get("session", {}).get("session_ref")
                if isinstance(ref, str) and ref:
                    ctx.memory.current_record = RecordAnchor(
                        domain="iretour" if "iretour" in name else "irego",
                        session_ref=ref,
                        evidence_id=evidence.evidence_id,
                        source_version=evidence.source_version,
                    )
            if result.status in {"succeeded", "partial"} and "patient_history" in name:
                ctx.memory.history_seen = True
                ctx.memory.history_domain = "iretour" if "iretour" in name else "irego"
                ctx.memory.history_page = data.get("page", {}).get("page_number", 1)
            if name.startswith("generate_") and result.status in {"succeeded", "partial"}:
                artifact = IReGoWorkflow._artifact(evidence)
                expiry = aware_date(artifact["expires_at"])
                if (expiry and expiry <= utcnow()) or not await ctx.backend.artifact_available(
                    artifact["url"]
                ):
                    raise DomainError(
                        "artifact_unavailable", "报告暂不可访问或已过期。", outcome="unavailable"
                    )
                result.outputs["artifact"] = artifact
            if result.status in {"failed", "unavailable"}:
                result.code = "tool_" + result.status
                result.message = body.get("patient_message") or "该项数据暂不可用。"
        artifact = result.outputs.get("artifact")
        if artifact:
            await ctx.events.artifact(ArtifactPayload.model_validate(artifact), task_id=tid)
        ctx.record.results[tid] = result
    # 错误兜底
    except asyncio.CancelledError:
        result.status, result.code = "cancelled", "request_cancelled"
        raise
    except (DomainError, TimeoutError) as exc:
        result.status = exc.outcome if isinstance(exc, DomainError) else "failed"
        result.code = exc.code if isinstance(exc, DomainError) else "tool_timeout"
        result.message = (
            exc.message
            if isinstance(exc, DomainError)
            else "本次工具查询超时，可尝试缩小范围或回答已有信息。"
        )
        result.retryable = exc.retryable if isinstance(exc, DomainError) else True
        body = {
            "tool_name": name,
            "status": result.status,
            "code": result.code,
            "patient_message": result.message,
            "retryable": result.retryable,
        }
    except EventPersistenceError:
        raise
    except Exception:
        result.status, result.code, result.message = (
            "failed",
            "tool_internal_error",
            "该项查询暂时失败，其他信息仍可使用。",
        )
        body = {
            "tool_name": name,
            "status": "failed",
            "code": result.code,
            "patient_message": result.message,
        }
    finally:
        result.elapsed_ms = (time.monotonic() - started) * 1000
        ctx.record.results[tid] = result
        if not ctx.emitter.persistence_failed:
            if result.status in {"failed", "unavailable", "unsupported", "clarification"}:
                await ctx.events.failure(
                    FailurePayload(
                        code=result.code or "tool_unavailable",
                        text=result.message or "该项查询暂不可用。",
                        retryable=result.retryable,
                    ),
                    task_id=tid,
                    goal_id="agent",
                )
            await ctx.repository.save_run(ctx.record)
    return compact_result(body, ctx.settings.agent_tool_output_tokens)
