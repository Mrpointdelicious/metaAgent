"""
创建日期：2026-10-11
文件功能：按原始路径投影患者业务事实，区分字段缺口并提供当前能力状态。
"""

import json

from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meta_agent.contracts import DomainError, fingerprint
from meta_agent.domains.facts import FactBuilder


class LayerModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")


class Page(LayerModel):
    page_number: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_items: int = Field(ge=0)
    has_next: bool


class Prescription(LayerModel):
    prescription_id: int
    name: str | None = None
    indication: str | None = None
    content: str | None = None
    offline_training: str | None = None
    online_evaluation: str | None = None


class MedicalRecord(LayerModel):
    record_id: int
    recorded_at: str | None = None
    medical_record: str | None = None
    content: dict
    prescriptions: list[Prescription] | None = None


class Consultation(LayerModel):
    patient_ref: str
    page: Page
    medical_record_count: int = Field(ge=0)
    consultation_count: int = Field(ge=0)
    medical_records: list[MedicalRecord]
    consultations: list[dict]
    limitations: list[str] = Field(default_factory=list)


class Plan(LayerModel):
    plan_id: int
    completion_flag: int | None = None
    status: int | None = None
    details: str | None = None
    scheduled_at: str | None = None


class Device(LayerModel):
    availability: str
    history: dict | None = None
    reason_codes: list[str] = Field(default_factory=list)
    patient_message: str | None = None


class Rehab(LayerModel):
    patient_ref: str
    plan_count: int = Field(ge=0)
    plans: list[Plan]
    plan_page: Page
    iretour: Device
    irego: Device
    limitations: list[str] = Field(default_factory=list)


def project_patient(name, evidence):
    """Return a separate display view; the original Evidence is never modified."""
    data = evidence.payload.get("data")
    schema = Consultation if name == "get_patient_consultation" else Rehab
    try:
        schema.model_validate(data)
    except ValidationError as exc:
        raise DomainError(
            "invalid_patient_contract",
            "本次患者接口结构不符合约定，不能解释为没有记录。",
            outcome="unavailable",
            retryable=False,
        ) from exc
    builder = FactBuilder(evidence)
    bounds = ["仅陈述本次查询范围；未提供、空值和空列表都不能证明患者不存在相应病史或处方。"]
    states = []
    if name == "get_patient_consultation":
        for i, record in enumerate(data["medical_records"]):
            prefix = f"/data/medical_records/{i}"
            builder.record_ref = f"medical:{record['record_id']}"
            builder.add(prefix + "/recorded_at", "recorded_at", "病历记录日期", required=True)
            for field, label in [
                ("doctor_orders", "该病历医嘱原文"),
                ("diagnosis", "该病历已记录诊断"),
            ]:
                builder.add(prefix + "/content/" + field, field, label, required=True)
            prescriptions = record.get("prescriptions")
            state = (
                "not_returned"
                if "prescriptions" not in record
                else "null"
                if prescriptions is None
                else "empty"
                if not prescriptions
                else "present"
            )
            states.append(
                {
                    "record_ref": builder.record_ref,
                    "path": prefix + "/prescriptions",
                    "state": state,
                    "meaning": "此病历返回的处方列表为空，不代表完整历史无处方。"
                    if state == "empty"
                    else "本次未提供可读取处方信息，不能说没有处方。"
                    if state != "present"
                    else "仅展示明确返回的处方原文。",
                }
            )
            for j, _prescription in enumerate(prescriptions or []):
                for field, label in [
                    ("name", "处方名称"),
                    ("content", "处方内容原文"),
                    ("indication", "记录的适应说明"),
                    ("offline_training", "记录的线下训练"),
                    ("online_evaluation", "记录的线上评估"),
                ]:
                    builder.add(f"{prefix}/prescriptions/{j}/{field}", field, label, required=True)
        bounds.append(
            "只有当返回记录明确提供否认内容时，才可在对应记录范围内引用；不能将缺失补成否定。"
        )
        paging = data["page"]
    else:
        builder.add("/data/plan_count", "plan_count", "本次数据源的计划数量", required=True)
        for i, plan in enumerate(data["plans"]):
            builder.record_ref = f"plan:{plan['plan_id']}"
            for field, label in [
                ("scheduled_at", "计划日期"),
                ("details", "返回的计划详情"),
                ("completion_flag", "计划完成标记（契约未定义取值含义）"),
            ]:
                builder.add(f"/data/plans/{i}/{field}", field, label, required=True)
        for device in ["iretour", "irego"]:
            builder.record_ref = None
            for field, label in [("availability", "可用状态"), ("patient_message", "覆盖说明")]:
                builder.add(f"/data/{device}/{field}", field, device + " " + label, required=True)
            history = data[device].get("history") or {}
            for i, _item in enumerate(history.get("items", [])):
                for field, label in [
                    ("session_ref", "供工具定位的训练引用"),
                    ("session_time", "历史训练日期"),
                    ("training_state", "历史训练状态"),
                    ("result_state", "历史结果状态"),
                ]:
                    builder.add(
                        f"/data/{device}/history/items/{i}/{field}", field, label, required=True
                    )
        bounds.append(
            "本契约未提供计划与训练的对应及依从性核验；不能说训练按计划进行。计划标记不替代实际结果。"
        )
        paging = data["plan_page"]
    body = {
        "tool_name": name,
        "status": "succeeded"
        if evidence.payload["status"] == "success"
        else evidence.payload["status"],
        "evidence_id": evidence.evidence_id,
        "business_view": True,
        "facts": [
            {
                "fact_id": v.fact.fact_id,
                "path": v.fact.path,
                "label": v.label,
                "value": v.fact.value,
                "value_status": v.fact.value_status,
            }
            for v in builder.views
        ],
        "interpretation": {
            "field_states": states,
            "limits": bounds + data.get("limitations", []),
            "page": paging,
        },
    }
    return body, builder.views


async def business_context(ctx, messages, tool_names):
    """Availability comes from the same tool gate; pagination requires fresh Evidence."""
    ids = []
    expired_knowledge = False
    for message in messages:
        if isinstance(message, ToolMessage) and isinstance(message.content, str):
            try:
                body = json.loads(message.content)
                if isinstance(body, dict) and body.get("evidence_id"):
                    ids.append(body["evidence_id"])
                if (
                    isinstance(body, dict)
                    and body.get("tool_name") == "search_knowledge"
                    and body.get("status") == "expired"
                ):
                    expired_knowledge = True
            except ValueError:
                continue
    ids.extend(
        eid
        for result in ctx.record.results.values()
        if result.status in {"succeeded", "partial"}
        for eid in result.evidence_ids
    )
    pages = {}
    knowledge_refs = set()
    for eid in ids:
        evidence = await ctx.repository.evidence(ctx.scope.scope_hash, eid)
        if not evidence or fingerprint(evidence.payload) != evidence.raw_payload_hash:
            continue
        if evidence.tool_name in {"knowledge.search", "knowledge.search.remote_rag"}:
            knowledge_refs.update(s["doc_ref"] for s in evidence.payload.get("snippets", []))
            continue
        if evidence.payload.get("status") not in {
            "success",
            "available",
            "partial",
        }:
            continue
        data = evidence.payload.get("data") or {}
        if not isinstance(data, dict):
            continue
        page = data.get("page")
        if isinstance(page, dict) and isinstance(page.get("has_next"), bool):
            pages[evidence.tool_name] = {
                "page_number": page.get("page_number"),
                "has_next": page["has_next"],
                "scope": "仅适用于这次查询范围；不限制用户发起不同条件的新查询",
            }
    return {
        "knowledge_tool_available": "search_knowledge" in tool_names,
        "knowledge_current_refs": sorted(knowledge_refs),
        "expired_knowledge_in_history": expired_knowledge,
        "patient_queries_available": sorted(
            name
            for name in tool_names
            if name.startswith("get_patient_") or name.endswith("_patient_history")
        ),
        "iretour_single_report_enabled": "generate_iretour_single_session_report" in tool_names,
        "iretour_trend_report_enabled": "generate_iretour_longitudinal_report" in tool_names,
        "record_anchor_available": bool(
            ctx.memory.current_record
            and await ctx.repository.evidence(
                ctx.scope.scope_hash, ctx.memory.current_record.evidence_id
            )
        ),
        "pages": pages,
        "plan_check": "当前患者层契约没有计划与训练对应核验，不得断言按计划执行。",
    }
