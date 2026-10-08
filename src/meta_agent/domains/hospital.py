"""
创建日期：2026-10-02
文件功能：适配医院运营查询和报表，只投影后端来源事实并校验制品。
"""

from pydantic import ValidationError

from meta_agent.contracts import DomainError, HospitalRequest, TaskResult, utcnow
from meta_agent.domains.facts import FactBuilder, aware_date, pointer_part
from meta_agent.domains.rehab import PatientEnvelope

LABELS = {
    "hospital_name": "医院",
    "patient_count": "患者数",
    "doctor_count": "医生数",
    "daily_active_count": "日活人数",
    "consultation_people": "问诊人数",
    "consultation_times": "问诊次数",
    "consultation_rooms": "问诊室数",
    "assessment_rooms": "评估室数",
    "training_rooms": "训练室数",
    "total_rooms": "房间总数",
    "assessment_count": "评估次数",
    "scale_assessment_count": "量表评估次数",
    "paradigm_assessment_count": "范式评估次数",
    "offline_training_count": "线下训练次数",
    "device_training_count": "设备训练次数",
    "corridor_training_count": "走廊训练次数",
    "time_scope": "统计口径",
    "medical_record_count": "病历数",
    "report_count": "报告数",
    "text": "运营分析",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "display_value": "指标值",
    "left_display_value": "左侧指标值",
    "right_display_value": "右侧指标值",
    "difference_display_value": "差值",
    "difference_definition": "差值口径",
}


class HospitalAdapter:
    async def execute(self, task, args, ctx):
        if ctx.scope.role != "operator":
            raise DomainError(
                "operator_required", "医院运营查询需要运营人员身份。", outcome="unsupported"
            )
        request = HospitalRequest.model_validate(args)
        if request.scope == "institution" and not (request.hospital_id or request.hospital_name):
            raise DomainError(
                "hospital_required", "请明确医院编号或名称。", outcome="clarification"
            )
        if request.scope == "comparison" and (
            len(set(request.hospital_ids)) != 2 or any(i <= 0 for i in request.hospital_ids)
        ):
            raise DomainError(
                "hospital_required", "请明确两个不同的医院编号。", outcome="clarification"
            )
        body = await ctx.call("query_hospital_operations", request.model_dump(exclude_none=True))
        try:
            envelope = PatientEnvelope.model_validate({"meta": {}, **body})
        except ValidationError as exc:
            raise DomainError("invalid_contract", "医院运营工具返回不符合契约。") from exc
        if (
            envelope.tool_name != "query_hospital_operations"
            or envelope.contract_version != "1.2.0"
        ):
            raise DomainError("invalid_contract", "医院运营工具名称或版本不匹配。")
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            envelope.tool_name,
            ctx.record.request_id,
            body,
            envelope.contract_version,
        )
        result = TaskResult(
            task_id=task.task_id,
            status={"success": "succeeded", "available": "succeeded"}.get(
                envelope.status, envelope.status
            ),
            evidence_ids=[evidence.evidence_id],
        )
        builder = FactBuilder(evidence)
        if result.status in {"failed", "unavailable"}:
            result.code, result.message = "tool_" + result.status, "医院运营数据暂不可用。"
            return result
        if envelope.data is None:
            raise DomainError("invalid_contract", "医院运营工具缺少数据。")

        def project(value, path, label=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"report", "hospital_id", "tenant_level", "code", "artifact_ref"}:
                        continue
                    project(item, f"{path}/{pointer_part(key)}", LABELS.get(key, key))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    project(item, f"{path}/{i}", label)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                builder.add(
                    path, path.rsplit("/", 1)[-1], label, required=label in {"统计口径", "差值口径"}
                )

        project(envelope.data, "/data")
        result.facts = builder.views
        report = envelope.data.get("report")
        if request.output_mode != "analysis":
            expiry = aware_date(report.get("expires_at")) if isinstance(report, dict) else None
            if (
                not isinstance(report, dict)
                or not report.get("artifact_ref")
                or not report.get("image_url")
            ):
                result.status, result.code, result.message = (
                    "partial",
                    "artifact_unavailable",
                    "运营数据已返回，报表暂不可用。",
                )
            elif (expiry and expiry <= utcnow()) or not await ctx.backend.artifact_available(
                report["image_url"]
            ):
                result.status, result.code, result.message = (
                    "partial",
                    "artifact_unavailable",
                    "运营报表暂不可访问。",
                )
            else:
                result.outputs["artifact"] = {
                    "artifact_ref": report["artifact_ref"],
                    "url": report["image_url"],
                    "expires_at": expiry.isoformat() if expiry else None,
                    "evidence_id": evidence.evidence_id,
                }
        return result
