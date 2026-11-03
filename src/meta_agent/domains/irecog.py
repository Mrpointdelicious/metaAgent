"""
创建日期：2026-10-20
文件功能：适配 IReCog 独立契约，执行固定查询和报表链并保留来源及跨轮记录边界。
Planner 只提交高层业务请求（operation/selector/need_artifact），内部工具链固定。
"""

from datetime import datetime, timedelta

from meta_agent.contracts import DomainError, IReCogRequest, TaskResult, utcnow
from meta_agent.domains.facts import FactBuilder, aware_date
from meta_agent.domains.irego import IReGoWorkflow
from meta_agent.domains.rehab import RehabAdapter, list_field, object_field

IRECOG_STATUS = {"success", "available", "partial"}


class IReCogAdapter(RehabAdapter):
    def project(self, task, evidence):
        body = evidence.payload
        status = {"success": "succeeded", "available": "succeeded"}.get(
            body["status"], body["status"]
        )
        result = TaskResult(
            task_id=task.task_id,
            status=status,
            evidence_ids=[evidence.evidence_id],
            outputs={"record_domain": "irecog", "source_version": evidence.source_version},
        )
        builder = FactBuilder(evidence)
        if body.get("patient_message"):
            builder.add("/patient_message", "patient_message", "说明", required=True)
        if status in {"failed", "unavailable"}:
            result.code = "tool_" + status
            result.message = "IReCog 暂无符合条件的可用数据。"
            result.facts = builder.views
            return result
        data = object_field(body, "data")
        result.outputs["source_snapshot"] = data.get("source_snapshot")
        endpoint = evidence.tool_name
        if endpoint == "get_irecog_patient_context":
            for key, label in {
                "record_count": "认知测评记录数",
                "readable_total_count": "可读总分记录数",
                "missing_result_count": "缺少结果记录数",
            }.items():
                builder.add(f"/data/counts/{key}", key, label, required=True)
            builder.add("/data/interpretation/summary", "summary", "解读摘要")
        elif endpoint == "get_irecog_patient_history":
            result.outputs["history_page"] = data.get("page_number", 1)
            sessions = list_field(data, "sessions")
            for i, item in enumerate(sessions):
                builder.record_ref = item.get("session_ref")
                for key, label in {
                    "recorded_at": "测评时间",
                    "result_state": "结果状态",
                    "instrument_title": "测评项目",
                    "reported_score": "源报告总分",
                }.items():
                    builder.add(
                        f"/data/sessions/{i}/{key}",
                        key,
                        f"第{i + 1}条{label}",
                        status="missing"
                        if key == "reported_score" and item.get("reported_score") is None
                        else "valid",
                        required=True,
                    )
                builder.observed_at = aware_date(item.get("recorded_at"))
            if not sessions:
                result.message = "当前页没有 IReCog 认知测评记录。"
        elif endpoint == "get_irecog_session_analysis":
            sessions = list_field(data, "sessions")
            if not sessions or not isinstance(sessions[0], dict):
                raise DomainError("invalid_contract", "IReCog 结果缺少测评记录。")
            session = sessions[0]
            ref = session.get("session_ref")
            if not isinstance(ref, str) or not ref:
                raise DomainError("invalid_contract", "IReCog 结果缺少记录引用。")
            builder.record_ref = ref
            builder.observed_at = aware_date(session.get("recorded_at"))
            for key, label in {
                "recorded_at": "测评时间",
                "result_state": "结果状态",
                "instrument_title": "测评项目",
                "reported_score": "源报告总分",
            }.items():
                builder.add(
                    f"/data/sessions/0/{key}",
                    key,
                    label,
                    status="missing"
                    if key == "reported_score" and session.get("reported_score") is None
                    else "valid",
                    required=True,
                )
            builder.add(
                "/data/sessions/0/score_meaning", "score_meaning", "总分语义", required=True
            )
            result.outputs.update(
                session_ref=ref,
                session_time=session.get("recorded_at"),
                analysis_evidence=evidence.evidence_id,
            )
        elif endpoint == "get_irecog_longitudinal_analysis":
            builder.add("/data/comparability", "comparability", "可比较性", required=True)
            sessions = list_field(data, "sessions")
            for i, item in enumerate(sessions):
                prefix = f"/data/sessions/{i}"
                builder.record_ref = item.get("session_ref")
                builder.observed_at = aware_date(item.get("recorded_at"))
                builder.add(
                    f"{prefix}/reported_score",
                    "reported_score",
                    f"第{i + 1}次源报告总分",
                    status="missing" if item.get("reported_score") is None else "valid",
                    uses=["display", "trend"],
                )
                builder.add(f"{prefix}/result_state", "result_state", f"第{i + 1}次结果状态")
        result.facts = builder.views
        return result


class IReCogWorkflow(IReGoWorkflow):
    def __init__(self):
        super().__init__(IReCogAdapter())

    async def execute(self, task, args, ctx):
        request = IReCogRequest.model_validate(args)
        if request.operation == "overview":
            evidence = await self._read(ctx, "get_irecog_patient_context", {})
            return self.adapter.project(task, evidence)
        if request.operation == "history":
            page = request.selector.count if request.selector.mode == "ordinal" else 1
            evidence = await self._read(
                ctx, "get_irecog_patient_history", {"page_number": page or 1}
            )
            return self.adapter.project(task, evidence)
        if request.operation == "trend":
            return await self._trend(task, request, ctx)
        return await self._session(task, request, ctx)

    async def _session(self, task, request, ctx):
        mode = request.selector.mode
        anchor = ctx.memory.current_record
        if mode in {"current_ref", "previous_record"} and (not anchor or anchor.domain != "irecog"):
            raise DomainError(
                "anchor_missing", "请先定位 IReCog 认知测评记录。", outcome="clarification"
            )
        if mode == "current_ref":
            evidence = await ctx.repository.evidence(ctx.scope.scope_hash, anchor.evidence_id)
            if (
                evidence
                and evidence.tool_name == "get_irecog_session_analysis"
                and evidence.source_version == anchor.source_version
            ):
                return await self._finish_irecog(
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
                    ctx, "get_irecog_patient_history", {"page_number": page, "page_size": 50}
                )
                if evidence.payload["status"] not in IRECOG_STATUS:
                    return self.adapter.project(task, evidence)
                data = object_field(evidence.payload, "data")
                candidates.extend(
                    item
                    for item in list_field(data, "sessions")
                    if not ((when := aware_date(item.get("recorded_at"))) and when > utcnow())
                )
                index = (request.selector.count or 1) - 1 if mode == "ordinal" else 0
                if mode == "previous_record":
                    position = next(
                        (
                            i
                            for i, item in enumerate(candidates)
                            if item.get("session_ref") == anchor.session_ref
                        ),
                        None,
                    )
                    index = position + 1 if position is not None else len(candidates)
                if index < len(candidates):
                    ref = candidates[index].get("session_ref")
                    break
                if not data.get("has_next"):
                    break
            if not ref:
                raise DomainError(
                    "record_not_found", "未找到可分析的 IReCog 记录。", outcome="unavailable"
                )
        else:
            raise DomainError(
                "unsupported_selector", "请明确单次认知测评记录。", outcome="clarification"
            )
        evidence = await self._read(
            ctx,
            "get_irecog_session_analysis",
            {
                "selector": "session_ref" if ref else "latest_usable",
                **({"session_ref": ref} if ref else {}),
            },
        )
        result = self.adapter.project(task, evidence)
        if (
            ref
            and result.status in {"succeeded", "partial"}
            and result.outputs.get("session_ref") != ref
        ):
            raise DomainError("record_mismatch", "IReCog 返回了不同的测评记录。")
        return await self._finish_irecog(task, request, result, ctx)

    async def _finish_irecog(self, task, request, result, ctx):
        ref = result.outputs.get("session_ref")
        if not isinstance(ref, str) or not ref:
            return await self._finish(
                task, request, result, ctx, endpoint="generate_irecog_single_session_report"
            )
        return await self._finish(
            task,
            request,
            result,
            ctx,
            endpoint="generate_irecog_single_session_report",
            report_payload={"selector": "session_ref", "session_ref": ref},
        )

    async def _trend(self, task, request, ctx):
        selector = request.selector
        args = {}
        if selector.mode in {"none", "latest_count"}:
            count = selector.count or 4
            if not 2 <= count <= 20:
                raise DomainError(
                    "invalid_window", "IReCog 趋势需要2到20次测评。", outcome="clarification"
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
                "invalid_window", "请明确连续测评次数或日期范围。", outcome="clarification"
            )
        evidence = await self._read(ctx, "get_irecog_longitudinal_analysis", {**args})
        return await self._finish(
            task,
            request,
            self.adapter.project(task, evidence),
            ctx,
            endpoint="generate_irecog_longitudinal_report",
            report_payload=args,
        )
