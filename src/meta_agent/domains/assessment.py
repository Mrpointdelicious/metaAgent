"""
创建日期：2026-10-20
文件功能：适配线上量表（Assessment）独立契约，执行固定查询和报表链并保留来源及跨轮记录边界。
报表端点要求携带同一轮分析的 expected_source_snapshot，由本工作流固定补链。
"""

from datetime import datetime, timedelta

from meta_agent.contracts import AssessmentRequest, DomainError, TaskResult, utcnow
from meta_agent.domains.facts import FactBuilder, aware_date
from meta_agent.domains.irego import IReGoWorkflow
from meta_agent.domains.rehab import RehabAdapter, list_field, object_field

ASSESSMENT_STATUS = {"success", "available", "partial"}


def _score_status(item: dict) -> str:
    return "missing" if item.get("reported_score") is None else "valid"


class AssessmentAdapter(RehabAdapter):
    def project(self, task, evidence):
        body = evidence.payload
        status = {"success": "succeeded", "available": "succeeded"}.get(
            body["status"], body["status"]
        )
        result = TaskResult(
            task_id=task.task_id,
            status=status,
            evidence_ids=[evidence.evidence_id],
            outputs={"record_domain": "assessment", "source_version": evidence.source_version},
        )
        builder = FactBuilder(evidence)
        if body.get("patient_message"):
            builder.add("/patient_message", "patient_message", "说明", required=True)
        if status in {"failed", "unavailable"}:
            result.code = "tool_" + status
            result.message = "量表暂无符合条件的可用数据。"
            result.facts = builder.views
            return result
        data = object_field(body, "data")
        snapshot = data.get("source_snapshot")
        if isinstance(snapshot, str) and len(snapshot) == 64:
            result.outputs["source_snapshot"] = snapshot
        endpoint = evidence.tool_name
        if endpoint == "get_assessment_patient_context":
            for key, label in {
                "record_count": "量表记录数",
                "readable_total_count": "可读总分记录数",
                "missing_result_count": "缺少结果记录数",
            }.items():
                builder.add(f"/data/counts/{key}", key, label, required=True)
            for i, _coverage in enumerate(list_field(data, "scale_coverage")):
                builder.add(
                    f"/data/scale_coverage/{i}/scale_name",
                    "scale_name",
                    "量表名称",
                    required=True,
                )
                builder.add(
                    f"/data/scale_coverage/{i}/record_count",
                    "record_count",
                    "记录数",
                    required=True,
                )
                builder.add(
                    f"/data/scale_coverage/{i}/readable_total_count",
                    "readable_total_count",
                    "可读总分记录数",
                )
            builder.add("/data/interpretation/summary", "summary", "解读摘要")
        elif endpoint == "get_assessment_patient_history":
            result.outputs["history_page"] = data.get("page_number", 1)
            records = list_field(data, "records")
            for i, item in enumerate(records):
                builder.record_ref = item.get("assessment_ref")
                builder.observed_at = aware_date(item.get("recorded_at"))
                for key, label in {
                    "recorded_at": "测评时间",
                    "scale_name": "量表名称",
                    "result_state": "结果状态",
                    "reported_score": "源报告总分",
                }.items():
                    builder.add(
                        f"/data/records/{i}/{key}",
                        key,
                        f"第{i + 1}条{label}",
                        status=_score_status(item) if key == "reported_score" else "valid",
                        required=True,
                    )
            if not records:
                result.message = "当前页没有量表记录。"
        elif endpoint == "get_assessment_result_analysis":
            records = list_field(data, "records")
            if not records:
                raise DomainError("invalid_contract", "量表结果缺少记录。")
            item = records[0]
            ref = item.get("assessment_ref")
            if not isinstance(ref, str) or not ref:
                raise DomainError("invalid_contract", "量表结果缺少记录引用。")
            builder.record_ref = ref
            builder.observed_at = aware_date(item.get("recorded_at"))
            for key, label in {
                "recorded_at": "测评时间",
                "scale_name": "量表名称",
                "result_state": "结果状态",
                "reported_score": "源报告总分",
                "score_meaning": "总分语义",
            }.items():
                builder.add(
                    f"/data/records/0/{key}",
                    key,
                    label,
                    status=_score_status(item) if key == "reported_score" else "valid",
                    required=True,
                )
            result.outputs.update(
                assessment_ref=ref,
                session_ref=ref,
                session_time=item.get("recorded_at"),
                analysis_evidence=evidence.evidence_id,
            )
        elif endpoint == "get_assessment_longitudinal_analysis":
            builder.add("/data/comparability", "comparability", "可比较性", required=True)
            records = list_field(data, "records")
            for i, item in enumerate(records):
                prefix = f"/data/records/{i}"
                builder.record_ref = item.get("assessment_ref")
                builder.observed_at = aware_date(item.get("recorded_at"))
                builder.add(
                    f"{prefix}/reported_score",
                    "reported_score",
                    f"第{i + 1}次源报告总分",
                    status=_score_status(item),
                    uses=["display", "trend"],
                )
                builder.add(f"{prefix}/result_state", "result_state", f"第{i + 1}次结果状态")
        result.facts = builder.views
        return result


class AssessmentWorkflow(IReGoWorkflow):
    def __init__(self):
        super().__init__(AssessmentAdapter())

    async def execute(self, task, args, ctx):
        request = AssessmentRequest.model_validate(args)
        if request.operation == "overview":
            evidence = await self._read(
                ctx, "get_assessment_patient_context", self._scale_args(request)
            )
            return self.adapter.project(task, evidence)
        if request.operation == "history":
            page = request.selector.count if request.selector.mode == "ordinal" else 1
            evidence = await self._read(
                ctx,
                "get_assessment_patient_history",
                {"page_number": page or 1, **self._scale_args(request)},
            )
            return self.adapter.project(task, evidence)
        if request.operation == "trend":
            return await self._trend(task, request, ctx)
        return await self._session(task, request, ctx)

    @staticmethod
    def _scale_args(request: AssessmentRequest) -> dict:
        return {"scale_type": request.scale_type} if request.scale_type else {}

    async def _session(self, task, request, ctx):
        mode = request.selector.mode
        anchor = ctx.memory.current_record
        if mode in {"current_ref", "previous_record"} and (
            not anchor or anchor.domain != "assessment"
        ):
            raise DomainError("anchor_missing", "请先定位量表记录。", outcome="clarification")
        if mode == "current_ref":
            evidence = await ctx.repository.evidence(ctx.scope.scope_hash, anchor.evidence_id)
            if (
                evidence
                and evidence.tool_name == "get_assessment_result_analysis"
                and evidence.source_version == anchor.source_version
            ):
                return await self._finish_assessment(
                    task, request, self.adapter.project(task, evidence), ctx
                )
            ref = anchor.session_ref
        elif mode == "latest_usable":
            ref = None
        elif mode in {"none", "latest_record", "previous_record", "ordinal"}:
            candidates = []
            ref = None
            for page in range(1, 4):
                evidence = await self._read(
                    ctx,
                    "get_assessment_patient_history",
                    {"page_number": page, "page_size": 50, **self._scale_args(request)},
                )
                if evidence.payload["status"] not in ASSESSMENT_STATUS:
                    return self.adapter.project(task, evidence)
                data = object_field(evidence.payload, "data")
                candidates.extend(
                    item
                    for item in list_field(data, "records")
                    if not ((when := aware_date(item.get("recorded_at"))) and when > utcnow())
                )
                index = (request.selector.count or 1) - 1 if mode == "ordinal" else 0
                if mode == "previous_record":
                    position = next(
                        (
                            i
                            for i, item in enumerate(candidates)
                            if item.get("assessment_ref") == anchor.session_ref
                        ),
                        None,
                    )
                    index = position + 1 if position is not None else len(candidates)
                if index < len(candidates):
                    ref = candidates[index].get("assessment_ref")
                    break
                if not data.get("has_next"):
                    break
            if not ref:
                raise DomainError(
                    "record_not_found", "未找到可分析的量表记录。", outcome="unavailable"
                )
        else:
            raise DomainError(
                "unsupported_selector", "请明确单次量表记录。", outcome="clarification"
            )
        evidence = await self._read(
            ctx,
            "get_assessment_result_analysis",
            {
                "selector": "assessment_ref" if ref else "latest_usable",
                **({"assessment_ref": ref} if ref else {}),
            },
        )
        result = self.adapter.project(task, evidence)
        if (
            ref
            and result.status in {"succeeded", "partial"}
            and result.outputs.get("assessment_ref") != ref
        ):
            raise DomainError("record_mismatch", "量表返回了不同的记录。")
        return await self._finish_assessment(task, request, result, ctx)

    async def _finish_assessment(self, task, request, result, ctx):
        ref = result.outputs.get("assessment_ref")
        if not isinstance(ref, str) or not ref:
            return await self._finish(
                task, request, result, ctx, endpoint="generate_assessment_single_report"
            )
        return await self._finish(
            task,
            request,
            result,
            ctx,
            endpoint="generate_assessment_single_report",
            report_payload={"selector": "assessment_ref", "assessment_ref": ref},
        )

    async def _trend(self, task, request, ctx):
        selector = request.selector
        args = {**self._scale_args(request)}
        if selector.mode in {"none", "latest_count"}:
            count = selector.count or 4
            if not 2 <= count <= 20:
                raise DomainError(
                    "invalid_window", "量表趋势需要2到20条记录。", outcome="clarification"
                )
            args.update(selection_mode="latest_count", report_count=count)
        elif selector.mode == "date_range":
            try:
                start, end = (
                    datetime.fromisoformat(selector.start),
                    datetime.fromisoformat(selector.end),
                )
                valid = start.tzinfo is not None and end.tzinfo is not None and start <= end
                if not valid or (end - start) > timedelta(days=180):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise DomainError(
                    "invalid_window",
                    "请提供含时区且不超过180天的有效日期范围。",
                    outcome="clarification",
                ) from exc
            args.update(
                selection_mode="date_range", start_date=selector.start, end_date=selector.end
            )
        else:
            raise DomainError(
                "invalid_window", "请明确连续记录数或日期范围。", outcome="clarification"
            )
        evidence = await self._read(ctx, "get_assessment_longitudinal_analysis", {**args})
        return await self._finish(
            task,
            request,
            self.adapter.project(task, evidence),
            ctx,
            endpoint="generate_assessment_longitudinal_report",
            report_payload=args,
        )

    async def _finish(self, task, request, result, ctx, *, endpoint, report_payload=None):
        """量表报表要求 expected_source_snapshot，从本次分析证据固定补链。"""
        if not request.need_artifact or result.status not in {"succeeded", "partial"}:
            return await super()._finish(
                task, request, result, ctx, endpoint=endpoint, report_payload=report_payload
            )
        snapshot = result.outputs.get("source_snapshot")
        if not isinstance(snapshot, str) or len(snapshot) != 64:
            if result.status == "succeeded":
                result.status = "partial"
            result.code = "invalid_contract"
            result.message = "量表分析未返回有效的来源快照，无法生成报表。"
            result.retryable = False
            return result
        return await super()._finish(
            task,
            request,
            result,
            ctx,
            endpoint=endpoint,
            report_payload={**(report_payload or {}), "expected_source_snapshot": snapshot},
        )
