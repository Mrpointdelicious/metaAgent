"""
创建日期：2026-10-02
文件功能：适配医院运营查询和报表，只投影后端来源事实并校验制品。
"""

import datetime as dt
import re

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

# 快照类 section 只反映当前/上游快照，不支持指定历史区间（对应 Dify 的 unsupported_time_window）。
SNAPSHOT_SECTIONS = {"overview", "rooms", "assessment_training", "records"}


def _resolve_window(date_mode, start_date, end_date, today):
    """把相对日期窗口映射为具体 (start, end)，Asia/Shanghai 语义（照搬 Dify period）。"""
    now = dt.date.fromisoformat(today)
    first = now.replace(day=1)
    if date_mode in (None, "this_month"):
        start, end = first, now
    elif date_mode == "last_month":
        end = first - dt.timedelta(days=1)
        start = end.replace(day=1)
    elif date_mode == "last_7_days":
        start, end = now - dt.timedelta(days=6), now
    elif date_mode == "last_30_days":
        start, end = now - dt.timedelta(days=29), now
    elif date_mode == "today":
        start = end = now
    elif date_mode == "yesterday":
        start = end = now - dt.timedelta(days=1)
    elif date_mode == "range":
        if not all(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v or "")) for v in (start_date, end_date)
        ):
            raise DomainError(
                "invalid_request",
                "请同时提供开始和结束日期，例如 2026-09-01 至 2026-09-14。",
                outcome="clarification",
            )
        start, end = dt.date.fromisoformat(start_date), dt.date.fromisoformat(end_date)
    else:
        raise DomainError(
            "invalid_request",
            "暂时无法识别日期，请提供明确的开始和结束日期。",
            outcome="clarification",
        )
    if end < start or (end - start).days > 366 or end > now:
        raise DomainError(
            "invalid_request",
            "请提供有效日期：结束日期不早于开始日期、不晚于今天，日期差不超过 366 天。",
            outcome="clarification",
        )
    return start.isoformat(), end.isoformat()


class HospitalAdapter:
    async def directory(self, task, args, ctx):
        """返回可用机构目录，支持按名称关键字过滤，供 LLM 匹配机构名称与编号。"""
        entries = await ctx.backend.directory()
        name = (args or {}).get("name")
        if name:
            needle = str(name).strip()
            entries = [e for e in entries if needle in e["hospital_name"]]
        return TaskResult(
            task_id=task.task_id,
            status="succeeded",
            outputs={"directory": entries, "total": len(entries)},
        )

    async def execute(self, task, args, ctx):
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
        # 目录兜底校验：编号必须来自真实目录，防止 LLM 跳过目录工具直接编造 id。
        directory = {entry["hospital_id"] for entry in await ctx.backend.directory()}
        if request.hospital_id is not None and request.hospital_id not in directory:
            raise DomainError(
                "hospital_required", "机构编号不在目录中，请重新查询机构列表。", outcome="clarification"
            )
        if request.hospital_ids and any(i not in directory for i in request.hospital_ids):
            raise DomainError(
                "hospital_required", "机构编号不在目录中，请重新查询机构列表。", outcome="clarification"
            )
        # 快照类 section 不支持历史区间；相对日期窗口由代码按 Asia/Shanghai 计算。
        if request.section in SNAPSHOT_SECTIONS and request.date_mode not in (
            None,
            "today",
            "this_month",
        ):
            raise DomainError(
                "invalid_request",
                "该类数据目前只有当前/上游快照，不能查询指定历史区间。可改查当前快照，或查询该区间的问诊人数和次数。",
                outcome="clarification",
            )
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        start, end = _resolve_window(
            request.date_mode, request.start_date, request.end_date, today
        )
        request.start_date = start
        request.end_date = end
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


# ---- 确定性渲染（对齐 Dify render 代码节点）：把查询结果渲染成固定 markdown 表格 ----

RENDER_SECTIONS = {
    "all": "综合运营情况",
    "overview": "机构概况",
    "consultation": "问诊情况",
    "rooms": "诊室资源",
    "assessment_training": "评估与训练",
    "records": "业务资料",
    "analysis": "运营分析",
}

RENDER_FIELDS = {
    "overview": [
        ("patient_count", "注册患者", "人"),
        ("doctor_count", "医生", "人"),
        ("daily_active_count", "当前活跃患者", "人"),
    ],
    "consultation": [
        ("consultation_people", "区间问诊人数", "人"),
        ("consultation_times", "区间问诊次数", "次"),
    ],
    "rooms": [
        ("consultation_rooms", "问诊室", "间"),
        ("assessment_rooms", "评估室", "间"),
        ("training_rooms", "训练室", "间"),
        ("total_rooms", "诊室合计", "间"),
    ],
    "assessment_training": [
        ("assessment_count", "评估次数", "次"),
        ("scale_assessment_count", "量表评估", "次"),
        ("paradigm_assessment_count", "范式评估", "次"),
        ("offline_training_count", "离线训练", "次"),
        ("device_training_count", "设备训练", "次"),
        ("corridor_training_count", "通道训练", "次"),
    ],
    "records": [
        ("real_consultation_count", "真人问诊", "次"),
        ("ai_consultation_count", "AI 问诊", "次"),
        ("remote_consultation_count", "远程会诊", "次"),
        ("medical_record_count", "就诊记录", "条"),
        ("prescription_template_count", "处方模板", "条"),
        ("report_count", "报告", "份"),
    ],
}


def hospital_report_urls(response: dict) -> str | None:
    """校验并返回报表图片 image_url；不是合法医院运营报表制品则返回 None。

    仅当 image_url 指向报表制品路径时才返回，避免把异常值当 [Cweb] 下发。
    完整报告 html 不进入答复文本，[web] 由后处理层统一拼「对话记录页」。
    """
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if response.get("status") != "success" or not isinstance(data, dict):
        return None
    report = data.get("report")
    if not isinstance(report, dict):
        return None
    image_url = report.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
        return None
    if "/api/ai/hospital-operations/report-artifacts/" not in image_url:
        return None
    return image_url


def render_hospital_markdown(response: dict) -> str | None:
    """把 query_hospital_operations 的原始契约渲染成确定性 markdown；无法渲染返回 None。"""
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if response.get("status") != "success" or not isinstance(data, dict):
        return None
    hospitals = data.get("hospitals")
    if not isinstance(hospitals, list) or not hospitals:
        return None
    scope = data.get("scope")
    section = data.get("section") or "all"
    period = data.get("period") if isinstance(data.get("period"), dict) else {}
    start, end = period.get("start_date"), period.get("end_date")

    name_map: dict[int, str] = {}
    for snap in hospitals:
        hospital = snap.get("hospital") if isinstance(snap, dict) else None
        if isinstance(hospital, dict) and isinstance(hospital.get("hospital_id"), int):
            name_map[hospital["hospital_id"]] = str(hospital.get("hospital_name") or "")
    names = [name for name in name_map.values() if name]
    if not names:
        return None

    lines = ["**" + " / ".join(names) + "**", "", f"查询内容：{RENDER_SECTIONS.get(section, section)}"]
    if section in ("all", "analysis", "consultation") and start and end:
        lines.append(f"问诊统计区间：{start} 至 {end}")
    if section != "consultation":
        lines.append("机构概况、诊室、评估训练及业务资料按当前/上游快照展示，不代表查询日期区间的新增数量。")
    lines.append("")

    for snap in hospitals:
        if not isinstance(snap, dict):
            continue
        hospital = snap.get("hospital") if isinstance(snap.get("hospital"), dict) else {}
        name = str(hospital.get("hospital_name") or "")
        lines.extend(["**" + name + "**", "", "| 指标 | 接口返回值 |", "|---|---:|"])
        projected: set[str] = set()
        for sec, fields in RENDER_FIELDS.items():
            if section not in ("all", "analysis") and sec != section:
                continue
            values = snap.get(sec)
            if not isinstance(values, dict):
                continue
            projected.add(sec)
            for key, label, unit in fields:
                value = values.get(key)
                if type(value) not in (int, float):
                    continue
                lines.append(f"| {label} | {value:,} {unit} |")
        summaries: list[str] = []
        if "overview" in projected:
            v = snap.get("overview") or {}
            summaries.append(
                f"注册患者 {v.get('patient_count', 0):,} 人，医生 {v.get('doctor_count', 0):,} 人，"
                f"当前活跃患者 {v.get('daily_active_count', 0):,} 人。"
            )
        if "consultation" in projected:
            v = snap.get("consultation") or {}
            summaries.append(
                f"所选日期区间内问诊 {v.get('consultation_people', 0):,} 人、"
                f"{v.get('consultation_times', 0):,} 次。"
            )
        if "rooms" in projected:
            v = snap.get("rooms") or {}
            summaries.append(f"当前诊室合计 {v.get('total_rooms', 0):,} 间。")
        if not summaries and "assessment_training" in projected:
            v = snap.get("assessment_training") or {}
            summaries.append(
                f"详情快照记录评估 {v.get('assessment_count', 0):,} 次、"
                f"离线训练 {v.get('offline_training_count', 0):,} 次。"
            )
        if not summaries and "records" in projected:
            v = snap.get("records") or {}
            summaries.append(
                f"详情快照记录就诊记录 {v.get('medical_record_count', 0):,} 条、"
                f"报告 {v.get('report_count', 0):,} 份。"
            )
        lines.extend(["", "**简要说明：**" + "".join(summaries), ""])

    if scope == "comparison":
        comparison = data.get("comparison")
        if isinstance(comparison, dict):
            left = comparison.get("left_hospital") if isinstance(comparison.get("left_hospital"), dict) else {}
            right = comparison.get("right_hospital") if isinstance(comparison.get("right_hospital"), dict) else {}
            left_name = name_map.get(left.get("hospital_id"), "")
            right_name = name_map.get(right.get("hospital_id"), "")
            if left_name and right_name and left.get("hospital_id") != right.get("hospital_id"):
                lines.extend(
                    [
                        "**两家机构对比**",
                        "",
                        f"| 指标 | {left_name} | {right_name} | 差值（右减左） |",
                        "|---|---:|---:|---:|",
                    ]
                )
                allowed = {key for fields in RENDER_FIELDS.values() for key, _, _ in fields}
                allowed.update(
                    ("daily_active_rate", "patients_per_doctor", "training_room_share", "ai_consultation_share")
                )
                for metric in comparison.get("metrics", []):
                    if not isinstance(metric, dict) or metric.get("code") not in allowed:
                        continue
                    if any(type(metric.get(k)) not in (int, float) for k in ("left_value", "right_value", "difference")):
                        continue
                    lines.append(
                        f"| {metric.get('label', '')} | {metric.get('left_display_value', '')} | "
                        f"{metric.get('right_display_value', '')} | {metric.get('difference_display_value', '')} |"
                    )
                lines.extend(["", "差值高低不直接代表运营质量的好坏。"])

    lines.extend(["", "数字按接口原值展示；0 表示接口返回 0。"])
    if hospital_report_urls(response):
        lines.extend(["", "可继续输入：换机构、改成上个月、和另一家对比。"])
    else:
        lines.extend(["", "可继续输入：换机构、改成上个月、和另一家对比、生成报告。"])
    return "\n".join(lines)
