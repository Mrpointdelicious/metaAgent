"""
创建日期：2026-08-29
文件功能：组合 IREGO 患者上下文、单次训练分析和按需报表接口。
"""

from typing import Any

from meta_agent.config import Settings
from meta_agent.orchestration.identity import TrustedScope, validate_patient_envelope
from meta_agent.orchestration.planner import TaskName
from meta_agent.tools.ai_webapi import AIWebApiClient
from meta_agent.tools.contracts import WorkflowOutcome


class IReGoWorkflow:
    """向编排层提供单一 IREGO 任务入口。"""

    def __init__(self, client: AIWebApiClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def execute(
        self,
        scope: TrustedScope,
        tasks: tuple[TaskName, ...],
    ) -> WorkflowOutcome:
        """执行最小必要接口并复用分析返回的稳定 session_ref。"""
        if self._settings.dry_run:
            return self._dry_run_outcome(tasks)

        async def post(endpoint, arguments):
            body = await self._client.post(endpoint, scope.patient_payload(arguments))
            validate_patient_envelope(scope, endpoint, body)
            return body

        raw: dict[str, Any] = {}
        facts: dict[str, Any] = {"status": "success"}
        patient_message = ""
        image_urls: list[str] = []
        mode_commands: list[str] = []

        if "patient_context" in tasks:
            context_body = await post(
                "get_multisource_patient_context",
                {
                    "projection_level": "compact",
                    "force_refresh": False,
                },
            )
            raw["patient_context"] = context_body
            facts.update(self._context_facts(context_body))
            patient_message = self._patient_message(context_body)

        if "session_analysis" in tasks:
            analysis_body = await post(
                "get_irego_session_analysis",
                {
                    "selector": "latest_usable",
                    "session_ref": None,
                    "detail_level": "standard",
                    "quality_detail": "summary",
                },
            )
            raw["session_analysis"] = analysis_body
            facts.update(self._analysis_facts(analysis_body))
            patient_message = self._patient_message(analysis_body) or patient_message

        if "single_report" in tasks:
            session_ref = str(facts.get("session_ref") or "")
            if session_ref:
                report_body = await post(
                    "generate_irego_single_session_report",
                    {
                        "session_ref": session_ref,
                        "expected_source_snapshot": (
                            raw.get("session_analysis", {}).get("data") or {}
                        ).get("source_snapshot"),
                    },
                )
                raw["single_report"] = report_body
                report_data = report_body.get("data") or {}
                image_url = str(report_data.get("image_url") or "").strip()
                if image_url:
                    image_urls.append(image_url)
                    mode_commands.append("rehab_report")
            else:
                facts["report_status"] = "unavailable"

        if not patient_message:
            patient_message = "暂时没有查到可用于回答的康复训练信息。"
        return WorkflowOutcome(
            patient_message=patient_message,
            facts=facts,
            raw_payload=raw,
            image_urls=image_urls,
            mode_commands=mode_commands,
        )

    @staticmethod
    def _dry_run_outcome(tasks: tuple[TaskName, ...]) -> WorkflowOutcome:
        wants_report = "single_report" in tasks
        return WorkflowOutcome(
            patient_message="已完成康复训练信息查询，这是框架联调返回，未读取真实患者数据。",
            facts={
                "status": "success",
                "completion": "unknown",
                "training_items": [],
                "available_actions": ["查询训练详情", "生成训练报告"],
            },
            raw_payload={"dry_run": True, "tasks": list(tasks)},
            image_urls=["https://example.invalid/report.png"] if wants_report else [],
            mode_commands=["rehab_report"] if wants_report else [],
        )

    @staticmethod
    def _patient_message(body: dict[str, Any]) -> str:
        return str(body.get("patient_message") or "").strip()

    @staticmethod
    def _context_facts(body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("data") or {}
        summary = (data.get("data_domains") or {}).get("irego") or {}
        return {
            "status": str(body.get("status") or "unknown"),
            "training_items": summary.get("training_items") or [],
            "available_actions": data.get("available_actions") or [],
        }

    @staticmethod
    def _analysis_facts(body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("data") or {}
        session = data.get("session") or {}
        return {
            "status": str(body.get("status") or "unknown"),
            "training_date": (data.get("time") or {}).get("report_recorded_at"),
            "completion": (data.get("time") or {}).get("training_state"),
            "training_items": data.get("report_blocks") or [],
            "session_ref": session.get("session_ref"),
            "capability_evaluations": data.get("capability_evaluations") or [],
            "record_tenant": session.get("record_tenant"),
            "available_actions": data.get("available_actions") or [],
        }
