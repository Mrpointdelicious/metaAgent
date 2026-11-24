"""
创建日期：2026-11-17
文件功能：将稳定工具请求映射到 AI_WebApi，保留已有患者 v2 身份与数据范围。

注意：患者身份装填（scope.patient_payload）与封套校验仍由 `RunContext.call` 负责，
这里只做「工具名 → 传输」的映射，避免同一份 payload 被注入两次。
"""

from meta_agent.medical_tools.contracts import (
    HOSPITAL_METRICS,
    ToolContext,
    ToolRequest,
    ToolResult,
)
from meta_agent.medical_tools.errors import ToolError
from meta_agent.medical_tools.registry import DIRECTORY_ENDPOINT, PATIENT_ENDPOINTS
from meta_agent.orchestration.identity import validate_patient_envelope


class AIWebApiAdapter:
    def __init__(self, backend) -> None:
        self.backend = backend

    async def execute(self, request: ToolRequest, context: ToolContext | None) -> ToolResult:
        grant = (
            context.scope.resource_grant if context and context.scope.role == "operator" else None
        )
        if request.name == DIRECTORY_ENDPOINT:
            entries = await self.backend.directory()
            if grant:
                entries = [
                    {"hospital_id": grant.hospital_id, "hospital_name": row["hospital_name"]}
                    for row in entries
                    if type(row.get("hospital_id")) is int
                    and row["hospital_id"] == grant.hospital_id
                    and isinstance(row.get("hospital_name"), str)
                ]
            return ToolResult(tool_name=request.name, payload={"directory": entries})
        payload = request.payload()
        if context and request.name in PATIENT_ENDPOINTS:
            payload = context.scope.patient_payload(payload, context.conversation_id)
        body = await self.backend.post(request.name, payload)
        if grant and request.name == "query_hospital_operations":
            body = scoped_hospital_response(body, grant, payload)
        if context and request.name in PATIENT_ENDPOINTS:
            validate_patient_envelope(
                context.scope,
                request.name,
                body,
                binding=request.name == "resolve_patient_identity",
            )
        return ToolResult(tool_name=request.name, payload=body)


def scoped_hospital_response(body, grant, query):
    section = query.get("section", "all")
    envelope = {
        key: body[key]
        for key in ("tool_name", "contract_version", "request_id", "status")
        if key in body
    }
    if body.get("status") in {"failed", "unavailable"}:
        return {**envelope, "data": None}
    data = body.get("data")
    rows = data.get("hospitals") if isinstance(data, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
        or not isinstance(rows[0].get("hospital"), dict)
        or type(rows[0]["hospital"].get("hospital_id")) is not int
        or rows[0]["hospital"]["hospital_id"] != grant.hospital_id
        or data.get("scope") != "institution"
        or data.get("section") != section
        or data.get("comparison")
    ):
        raise ToolError("resource_mismatch", "上游未确认授权机构和指标，已停止使用。")
    row = rows[0]
    sections = (
        HOSPITAL_METRICS
        if "all" in grant.sections and section in ("all", "analysis")
        else (section,)
    )
    hospital = {"hospital_id": grant.hospital_id}
    if isinstance(row["hospital"].get("hospital_name"), str):
        hospital["hospital_name"] = row["hospital"]["hospital_name"]
    metrics = {}
    for key in sections:
        values = row.get(key)
        if isinstance(values, dict) and key in HOSPITAL_METRICS:
            metrics[key] = {
                name: values[name]
                for name, _, _ in HOSPITAL_METRICS[key]
                if name in values and (values[name] is None or type(values[name]) in (int, float))
            }
    period = data.get("period")
    report = data.get("report")
    allow_report = "all" in grant.sections and query.get("output_mode") in {
        "report",
        "analysis_and_report",
    }
    analysis = data.get("analysis")
    projection = {
        "scope": "institution",
        "section": section,
        "period": {
            key: period[key]
            for key in ("start_date", "end_date")
            if isinstance(period, dict) and isinstance(period.get(key), str)
        },
        "hospitals": [{"hospital": hospital, **metrics}],
        "analysis": [
            {"text": entry["text"]}
            for entry in analysis
            if isinstance(entry, dict) and isinstance(entry.get("text"), str)
        ]
        if section in {"all", "analysis"} and isinstance(analysis, list)
        else [],
        "report": {
            key: report[key]
            for key in ("artifact_ref", "image_url", "expires_at")
            if isinstance(report.get(key), str)
        }
        if allow_report and isinstance(report, dict)
        else None,
    }
    return {**envelope, "data": projection}
