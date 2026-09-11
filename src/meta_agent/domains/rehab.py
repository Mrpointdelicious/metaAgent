"""
创建日期：2026-09-08
文件功能：实现1.6.0患者工具封套、真实记录选择、事实投影与报表制品核验。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, Selector, TaskResult, TaskSpec, fingerprint, utcnow
from meta_agent.domains.facts import FactBuilder, aware_date, pointer_part
from meta_agent.infrastructure.repository import EvidenceEnvelope


class PatientEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    tool_name: str
    contract_version: str
    request_id: str
    status: Literal["success", "available", "partial", "unavailable", "failed"]
    data: dict[str, Any] | None
    meta: dict[str, Any]
    patient_message: str | None = None
    reason_codes: list[str] = []


def object_field(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise DomainError("invalid_contract", f"工具结果缺少有效的{key}字段。")
    return value


def list_field(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DomainError("invalid_contract", f"工具结果缺少有效的{key}列表。")
    return value


class RehabAdapter:
    ENDPOINTS = {"rehab.overview": "get_multisource_patient_context",
        "rehab.history": "get_irego_patient_history", "rehab.session": "get_irego_session_analysis",
        "rehab.trend": "get_irego_longitudinal_analysis",
        "rehab.single_report": "generate_irego_single_session_report",
        "rehab.trend_report": "generate_irego_longitudinal_report"}

    async def fetch(self, ctx: RunContext, endpoint: str, args: dict[str, Any]) -> EvidenceEnvelope:
        if not ctx.scope.patient_id:
            raise DomainError("patient_required", "请先绑定患者身份。", outcome="clarification")
        body = await ctx.call(endpoint, {**args, "system_context": {
            "user": ctx.scope.patient_id, "conversation_id": ctx.record.conversation_id}})
        try:
            envelope = PatientEnvelope.model_validate(body)
        except ValidationError as exc:
            raise DomainError("invalid_contract", "工具返回不符合患者接口契约。") from exc
        if envelope.contract_version != "1.6.0":
            raise DomainError("unsupported_contract", "患者工具契约版本尚未支持。")
        if envelope.tool_name != endpoint:
            raise DomainError("wrong_tool_result", "工具返回与请求能力不一致。")
        return await ctx.repository.save_evidence(ctx.scope.scope_hash, endpoint,
            ctx.record.request_id, body, envelope.contract_version)

    def project(self, task: TaskSpec, evidence: EvidenceEnvelope) -> TaskResult:
        body = evidence.payload
        status = {"success":"succeeded", "available":"succeeded", "partial":"partial",
                  "unavailable":"unavailable", "failed":"failed"}[body["status"]]
        result = TaskResult(task_id=task.task_id, status=status, evidence_ids=[evidence.evidence_id])
        if status in {"failed", "unavailable"}:
            result.code = "tool_" + body["status"]
            result.message = "工具无法提供该结果。" if status == "unavailable" else "工具业务处理失败。"
        data = body.get("data")
        if not isinstance(data, dict):
            if status not in {"failed", "unavailable"}:
                raise DomainError("invalid_contract", "工具标记成功但没有结构化结果。")
            builder = FactBuilder(evidence)
            if body.get("patient_message"):
                builder.add("/patient_message", "patient_message", "说明", required=True)
            result.facts = builder.views
            return result
        endpoint = evidence.tool_name
        builder = FactBuilder(evidence)
        if body.get("patient_message"):
            builder.add("/patient_message", "patient_message", "说明", required=True)
        if endpoint == "get_multisource_patient_context":
            profile = object_field(data, "profile_brief")
            facts = object_field(profile, "facts")
            domains = object_field(data, "data_domains")
            irego = object_field(domains, "irego")
            for key in facts:
                builder.add(f"/data/profile_brief/facts/{pointer_part(key)}", key, key, topic="profile")
            builder.add("/data/data_domains/irego/availability", "availability", "IREGO数据可用性", required=True)
            for key, label in {"report_count":"训练报告数", "plan_count":"训练计划数"}.items():
                if key in (irego.get("counts") or {}):
                    builder.add(f"/data/data_domains/irego/counts/{key}", key, label)
            builder.quality("/data/data_domains/irego/quality")
        elif endpoint == "get_irego_patient_history":
            items = list_field(data, "items")
            page = object_field(data, "page")
            result.outputs.update(history_page=page.get("page_number", 1), has_results=bool(items))
            for i, item in enumerate(items):
                prefix = f"/data/items/{i}"
                builder.record_ref = item.get("session_ref")
                builder.observed_at = aware_date(item.get("session_time"))
                for field, label in [("session_time", "记录时间"), ("training_state", "训练状态"),
                                     ("patient_message", "记录说明")]:
                    builder.add(f"{prefix}/{field}", field, f"第{i+1}条{label}", required=True)
                for j, _ in enumerate(item.get("display_names") or []):
                    builder.add(f"{prefix}/display_names/{j}", "training_name", f"第{i+1}条训练项目")
            if not items:
                result.message = "当前页没有训练记录。"
        elif endpoint == "get_irego_session_analysis":
            session, times = object_field(data, "session"), object_field(data, "time")
            ref = session.get("session_ref")
            if not isinstance(ref, str) or not ref:
                raise DomainError("invalid_contract", "单次结果缺少稳定记录引用。")
            builder.record_ref = ref
            time_field = next((key for key in ("completed_at", "report_recorded_at", "execution_started_at",
                              "source_record_created_at") if times.get(key)), "completed_at")
            builder.observed_at = aware_date(times.get(time_field))
            builder.add(f"/data/time/{time_field}", "session_time", "训练时间", required=True)
            builder.add("/data/time/training_state", "training_state", "训练状态", required=True)
            overview = object_field(data, "overview")
            builder.add("/data/overview/total_training_duration", "training_duration", "训练总时长",
                        unit=overview.get("duration_unit") or "unknown")
            builder.add("/data/overview/report_block_count", "report_block_count", "报告记录分段数")
            if "report_evaluation_facts" in data:
                builder.add("/data/report_evaluation_facts/completion_rate", "completion_rate", "完成率（比例）")
            builder.metrics(list_field(data, "report_blocks"))
            for i, facet in enumerate(data.get("current_performance_facets") or []):
                if facet.get("patient_message"):
                    builder.add(f"/data/current_performance_facets/{i}/patient_message", "current_performance",
                                str(facet.get("capability_name") or "本次表现"), topic="performance")
            builder.quality()
            result.outputs.update(session_ref=ref, session_time=times.get(time_field),
                source_version=evidence.source_version, record_completed=times.get("training_state") == "completed",
                analysis_evidence=evidence.evidence_id, report_available=bool(data.get("report_blocks")))
        elif endpoint == "get_irego_longitudinal_analysis":
            window = object_field(data, "window")
            for field, label in [("started_at", "窗口开始"), ("ended_at", "窗口结束"),
                    ("requested_report_count", "请求记录数"), ("resolved_report_count", "窗口记录数"),
                    ("comparison_mode", "比较模式"), ("is_contiguous", "连续窗口")]:
                builder.add(f"/data/window/{field}", field, label, required=True)
            builder.quality()
            for i, evaluation in enumerate(data.get("capability_evaluations") or []):
                for field, label in [("patient_message", "训练表现"), ("explanation_boundary", "解释范围")]:
                    if evaluation.get(field):
                        builder.add(f"/data/capability_evaluations/{i}/{field}", field,
                                    str(evaluation.get("capability_name") or label),
                                    required=field == "explanation_boundary", uses=["display", "trend"])
            for i, project in enumerate(data.get("projects") or []):
                for j, lane in enumerate(project.get("lanes") or []):
                    for k, series in enumerate(lane.get("metric_series") or []):
                        prefix = f"/data/projects/{i}/lanes/{j}/metric_series/{k}"
                        eligible = series.get("comparison_eligible") is True
                        for n, _ in enumerate(series.get("comparison_limitations") or []):
                            builder.add(f"{prefix}/comparison_limitations/{n}", "comparison_boundary",
                                        "比较限制", required=True)
                        if eligible and (series.get("interpretation") or {}).get("patient_message"):
                            builder.add(f"{prefix}/interpretation/patient_message", "trend_interpretation",
                                        str(series.get("display_name") or "趋势"), uses=["display", "trend"])
            result.outputs["window"] = window
            if window.get("comparison_mode") in {"unavailable", "not_comparable"}:
                result.status, result.message = "unavailable", "该窗口暂不满足比较条件。"
        result.facts = builder.views
        return result

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        if task.capability == "rehab.resolve_session":
            return await self.resolve(task, Selector.model_validate(args["selector"]), ctx)
        endpoint = self.ENDPOINTS[task.capability]
        if task.capability == "rehab.overview":
            args = {"projection_level": "compact", "force_refresh": args.get("force_refresh", False)}
        elif task.capability == "rehab.session":
            for parent in ctx.record.results.values():
                if (parent.outputs.get("session_ref") == args.get("session_ref")
                        and parent.outputs.get("analysis_evidence")):
                    evidence = await ctx.repository.evidence(ctx.scope.scope_hash, parent.outputs["analysis_evidence"])
                    if evidence:
                        return self.project(task, evidence)
            args = {**args, "detail_level": "standard", "quality_detail": "summary"}
        elif task.capability.startswith("rehab.trend"):
            args = {**args, "selector": "latest_contiguous", "project_scope": "all"}
            if task.capability == "rehab.trend":
                args.update(detail_level="standard", quality_detail="summary")
        elif task.capability == "rehab.history":
            args = {**args, "project_scope": "all"}
        cache_key = fingerprint([endpoint, args])
        if task.capability == "rehab.overview" and not args["force_refresh"]:
            cached = await ctx.repository.get("cache", ctx.scope.scope_hash, cache_key)
            if cached:
                evidence = await ctx.repository.evidence(ctx.scope.scope_hash, cached["evidence_id"])
                if evidence:
                    result = self.project(task, evidence)
                    result.outputs["cache_hit"] = True
                    return result
        evidence = await self.fetch(ctx, endpoint, args)
        result = self.project(task, evidence)
        if task.capability == "rehab.session" and args.get("session_ref") and result.outputs.get("session_ref") != args["session_ref"]:
            raise DomainError("record_mismatch", "工具返回了不同记录，本次结果不用于回答。")
        if task.effect == "prepare_artifact" and result.status in {"succeeded", "partial"}:
            data = object_field(evidence.payload, "data")
            ref, url = data.get("artifact_ref"), data.get("image_url")
            expiry = aware_date(data.get("expires_at"))
            if not isinstance(ref, str) or not ref or not isinstance(url, str) or not url:
                raise DomainError("invalid_artifact", "报表结果缺少制品引用或地址。")
            if expiry is not None and expiry <= utcnow() or not await ctx.backend.artifact_available(url):
                raise DomainError("artifact_unavailable", "报告暂不可访问或已过期。", outcome="unavailable")
            result.outputs["artifact"] = {"artifact_ref": ref, "url": url,
                "expires_at": expiry.isoformat() if expiry else None, "evidence_id": evidence.evidence_id}
            result.message = "报告已生成。"
        if task.capability == "rehab.overview" and result.status == "succeeded":
            await ctx.repository.put("cache", ctx.scope.scope_hash, cache_key,
                                     {"evidence_id": evidence.evidence_id}, ctx.settings.cache_ttl_seconds)
        return result

    async def resolve(self, task: TaskSpec, selector: Selector, ctx: RunContext) -> TaskResult:
        anchor = ctx.memory.current_record
        if selector.mode in {"current_ref", "latest_usable"}:
            if selector.mode == "current_ref" and not anchor:
                raise DomainError("anchor_missing", "请明确当前训练记录。", outcome="clarification")
            args = {"selector": "session_ref" if anchor and selector.mode == "current_ref" else "latest_usable",
                    "session_ref": anchor.session_ref if anchor and selector.mode == "current_ref" else None,
                    "detail_level": "standard", "quality_detail": "summary"}
            evidence = await self.fetch(ctx, "get_irego_session_analysis", args)
            result = self.project(task, evidence)
            if args["session_ref"] and result.outputs.get("session_ref") != args["session_ref"]:
                raise DomainError("anchor_expired", "原记录引用已失效，请重新定位同一条记录。", outcome="clarification")
            return result
        if selector.mode == "previous_record" and not anchor:
            raise DomainError("anchor_missing", "请先明确当前训练记录。", outcome="clarification")
        candidates: list[tuple[dict[str, Any], EvidenceEnvelope, int]] = []
        complete = False
        # Discovery is bounded and explicitly reports a missing anchor beyond the searched pages.
        for page in range(1, 4):
            evidence = await self.fetch(ctx, "get_irego_patient_history", {
                "page_number": page, "page_size": 50, "record_scope": "all",
                "project_scope": "all", "training_state": "all"})
            if evidence.payload["status"] not in {"success", "available", "partial"}:
                return self.project(task, evidence)
            data = object_field(evidence.payload, "data")
            items = list_field(data, "items")
            for index, item in enumerate(items):
                when = aware_date(item.get("session_time"))
                if item.get("training_state") == "voided" or when is not None and when > utcnow():
                    continue
                candidates.append((item, evidence, index))
            target_index = 0 if selector.mode in {"none", "latest_record"} else (selector.count or 1) - 1
            if selector.mode == "previous_record":
                position = next((i for i, (item, _, _) in enumerate(candidates)
                                 if item.get("session_ref") == anchor.session_ref), None)
                target_index = position + 1 if position is not None else len(candidates)
            if target_index < len(candidates):
                item, source, index = candidates[target_index]
                builder = FactBuilder(source, item.get("session_ref"), item.get("session_time"))
                for field, label in [("session_time", "记录时间"), ("training_state", "训练状态"),
                                     ("patient_message", "记录说明")]:
                    builder.add(f"/data/items/{index}/{field}", field, label, required=True)
                ref = item.get("session_ref")
                result = TaskResult(task_id=task.task_id, facts=builder.views, evidence_ids=[source.evidence_id],
                    outputs={"session_ref": ref, "session_time": item.get("session_time"),
                             "source_version": source.source_version,
                             "record_completed": item.get("training_state") == "completed"})
                if not ref:
                    result.status, result.code = "unavailable", "record_not_analyzable"
                    result.message = "已定位该记录，但它目前没有可用于解读的报告。"
                return result
            if not object_field(data, "page").get("has_next", False):
                complete = True
                break
        return TaskResult(task_id=task.task_id, status="unavailable" if complete else "clarification",
            code="record_not_found", message="未找到符合条件的记录。" if complete else
            "当前搜索范围内未定位到该记录，请明确日期或记录。")
