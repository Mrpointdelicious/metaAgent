from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings
from server.session_manager import AgentSessionManager
from tools import ToolSpec

from .agent_prompts import OPEN_ANALYTICS_AGENT_INSTRUCTIONS, build_open_analytics_agent_input
from .schemas import AgentAnalyticsResult, AgentToolCallRecord, OrchestratorRequest, RoutedDecision


logger = logging.getLogger(__name__)


class OpenAnalyticsAgentRuntime:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_turns: int = 12,
        session_manager: AgentSessionManager | None = None,
    ):
        self.settings = settings or get_settings()
        self.max_turns = max_turns
        self.session_manager = session_manager or AgentSessionManager(self.settings)

    def can_run(
        self,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
    ) -> bool:
        return mode == "agents_sdk" and llm_config.can_use_agents_sdk and bool(llm_config.model)

    def _session_for_request(self, request: OrchestratorRequest) -> Any:
        identity = request.identity_context
        conversation_id = str(identity.conversation_id).strip() if identity and identity.conversation_id else ""
        session_id = str(identity.session_id).strip() if identity and identity.session_id else ""
        sdk_session_key = conversation_id or session_id
        if not sdk_session_key:
            raise RuntimeError("agents_sdk_runtime.session_missing: identity_context.conversation_id or session_id is required")
        return self.session_manager.get_or_create_session(sdk_session_key)

    def run(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        tool_specs: list[ToolSpec],
        llm_config: ResolvedLLMConfig,
    ) -> AgentAnalyticsResult:
        if not self.can_run(mode="agents_sdk", llm_config=llm_config):
            raise RuntimeError("agents_sdk_runtime.disabled: provider/model credentials are incomplete")

        agent_tools = [tool.get_agent_tool() for tool in tool_specs if tool.get_agent_tool() is not None]
        if len(agent_tools) != len(tool_specs) or not agent_tools:
            raise RuntimeError("agents_sdk_runtime.disabled: not all tool specs have agent_tool")

        try:
            from agents import Agent, ModelSettings, RunConfig, Runner
            from agents.agent import ToolsToFinalOutputResult
            from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
            from agents.tool import FunctionToolResult
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("agents_sdk_runtime.disabled: agents SDK is not installed") from exc

        client = AsyncOpenAI(api_key=llm_config.api_key, base_url=llm_config.base_url)
        model = OpenAIChatCompletionsModel(model=llm_config.model, openai_client=client)
        captured_tool_results: list[FunctionToolResult] = []

        def finish_when_sufficient_tools_called(_context: Any, tool_results: list[FunctionToolResult]) -> ToolsToFinalOutputResult:
            captured_tool_results.extend(tool_results)
            payload = self._final_payload_from_tool_results(
                captured_tool_results,
                request=request,
                routed_decision=routed_decision,
            )
            if payload is None:
                return ToolsToFinalOutputResult(is_final_output=False)
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=json.dumps(payload, ensure_ascii=False),
            )

        agent = Agent(
            name="Open Analytics Agent",
            instructions=OPEN_ANALYTICS_AGENT_INSTRUCTIONS,
            tools=agent_tools,
            model=model,
            model_settings=ModelSettings(temperature=0, parallel_tool_calls=False),
            tool_use_behavior=finish_when_sufficient_tools_called,
        )
        tool_catalog = [self._tool_catalog_entry(tool) for tool in tool_specs]
        run_input = build_open_analytics_agent_input(
            request=request,
            routed_decision=routed_decision,
            tool_catalog=tool_catalog,
        )

        run_config = RunConfig(
            tracing_disabled=not llm_config.tracing_enabled,
            trace_include_sensitive_data=False,
            workflow_name="open_analytics_agent",
        )
        agent_session = self._session_for_request(request)
        result = Runner.run_sync(
            agent,
            run_input,
            max_turns=self.max_turns,
            run_config=run_config,
            session=agent_session,
        )
        parsed = self._parse_final_output(
            result.final_output,
            request=request,
            routed_decision=routed_decision,
        )
        tool_calls = self._tool_calls_from_run_items(getattr(result, "new_items", []))
        parsed = parsed.model_copy(update={"tool_calls": tool_calls})

        structured = parsed.structured_output or {}
        if structured.get("fallback_required") is True or structured.get("status") in {"failed", "error", "unsupported"}:
            reason = structured.get("reason") or structured.get("summary") or "agent reported fallback_required"
            raise RuntimeError(f"agents_sdk_runtime.output_failed:{reason}")
        return parsed

    def _final_payload_from_tool_results(
        self,
        tool_results: list[Any],
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
    ) -> dict[str, Any] | None:
        entries = [
            {
                "tool_name": self._tool_name_from_function_result(item),
                "output": self._coerce_tool_output(getattr(item, "output", None)),
            }
            for item in tool_results
        ]
        subtype = routed_decision.final_subtype
        scope = routed_decision.final_scope
        tool_summaries = [
            {"tool_name": item["tool_name"], "output_summary": self._summarize_tool_output(item["output"])}
            for item in entries
        ]

        if scope == "doctor_aggregate":
            doctor_rows = self._last_output(entries, "list_doctors_with_active_plans")
            if doctor_rows is None:
                return None
            rows = doctor_rows if isinstance(doctor_rows, list) else doctor_rows.get("rows", []) if isinstance(doctor_rows, dict) else []
            summary = f"Found {len(rows)} doctors with active patient training plans."
            return self._agent_result_payload(
                request=request,
                routed_decision=routed_decision,
                summary=summary,
                result_rows=rows,
                tool_summaries=tool_summaries,
                rationale="Stopped after the doctor aggregate tool returned rows.",
            )

        ranked = self._last_output(entries, "rank_patients")
        if isinstance(ranked, dict):
            rows = ranked.get("rows") or []
            summary = f"Found and ranked {len(rows)} patients for the requested open analytics question."
            return self._agent_result_payload(
                request=request,
                routed_decision=routed_decision,
                summary=summary,
                result_rows=rows,
                tool_summaries=tool_summaries,
                rationale="Stopped after rank_patients returned ranked rows.",
            )

        diff = self._last_output(entries, "set_diff")
        if isinstance(diff, dict) and not (diff.get("patient_ids") or []):
            summary = "No patients matched the requested absent-patient set difference."
            return self._agent_result_payload(
                request=request,
                routed_decision=routed_decision,
                summary=summary,
                result_rows=[],
                tool_summaries=tool_summaries,
                rationale="Stopped after set_diff returned an empty patient set.",
            )
        if subtype in {"absent_from_baseline_window", "absent_old_patients_recent_window"} and diff is not None:
            return None
        return None

    def _agent_result_payload(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        summary: str,
        result_rows: list[Any],
        tool_summaries: list[dict[str, Any]],
        rationale: str,
    ) -> dict[str, Any]:
        return {
            "normalized_question": request.raw_text or "",
            "subtype": routed_decision.final_subtype,
            "scope": routed_decision.final_scope,
            "source": "agents_sdk_runtime",
            "final_text": summary,
            "structured_output": {
                "summary": summary,
                "result_rows": result_rows,
                "tool_result_summaries": tool_summaries,
                "fallback_required": False,
            },
            "tool_calls": [],
            "rationale": rationale,
        }

    def _tool_name_from_function_result(self, result: Any) -> str:
        tool = getattr(result, "tool", None)
        return str(getattr(tool, "name", None) or "unknown_tool")

    def _last_output(self, entries: list[dict[str, Any]], tool_name: str) -> Any | None:
        for item in reversed(entries):
            if item["tool_name"] == tool_name:
                return item["output"]
        return None

    def _coerce_tool_output(self, output: Any) -> Any:
        if isinstance(output, str):
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                return output
        return output

    def _tool_catalog_entry(self, tool: ToolSpec) -> dict[str, Any]:
        metadata = tool.metadata()
        notes: list[str] = []
        if tool.tool_name in {"list_patients_seen_by_doctor", "list_patients_with_active_plans"}:
            notes.append("Returns PatientSet JSON with set_id, patient_ids, patients, patient_names, and count. Save set_id for set_diff.")
            notes.append("Patient names are already enriched by the service layer; do not query dbuser.")
        if tool.tool_name == "set_diff":
            notes.append("Arguments must be base_set_id and subtract_set_id from previous PatientSet.set_id values.")
            notes.append("Use baseline set_id as base_set_id and recent set_id as subtract_set_id.")
        if tool.tool_name == "rank_patients":
            notes.append("Pass patient_ids from set_diff.patient_ids. Use top_k from explicit_slots.")
            notes.append("Supported strategy values are active_plan_but_absent, last_visit_oldest, and highest_risk.")
        if tool.tool_name == "list_doctors_with_active_plans":
            notes.append("Doctor aggregate tool. Do not pass doctor_id.")
            notes.append("Doctor names are already enriched by the service layer; do not query dbuser.")
        return {
            "tool_name": metadata["tool_name"],
            "description": metadata["description"],
            "input_schema": metadata["input_schema"],
            "chain_scope": metadata["chain_scope"],
            "notes": notes,
        }

    def _parse_final_output(
        self,
        output: Any,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
    ) -> AgentAnalyticsResult:
        if isinstance(output, AgentAnalyticsResult):
            return output
        if isinstance(output, dict):
            payload = output
        else:
            payload = self._json_payload_from_text(str(output or ""))
        normalized = self._normalize_payload(
            payload,
            request=request,
            routed_decision=routed_decision,
        )
        return AgentAnalyticsResult.model_validate(normalized)

    def _json_payload_from_text(self, text: str) -> dict[str, Any]:
        stripped = text.strip()
        if not stripped:
            raise ValueError("agents_sdk_runtime.empty_output")
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            stripped = fenced.group(1).strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                raise
            payload = json.loads(stripped[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("agents_sdk_runtime.output_not_object")
        return payload

    def _normalize_payload(
        self,
        payload: dict[str, Any],
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
    ) -> dict[str, Any]:
        normalized = dict(payload)
        structured = normalized.get("structured_output")
        if not isinstance(structured, dict):
            structured = {}
            for key in ("summary", "result_rows", "tool_result_summaries", "fallback_required", "status", "reason"):
                if key in normalized:
                    structured[key] = normalized[key]
        normalized["structured_output"] = structured
        normalized.setdefault("normalized_question", request.raw_text or "")
        normalized.setdefault("subtype", routed_decision.final_subtype)
        normalized.setdefault("scope", routed_decision.final_scope)
        normalized.setdefault("source", "agents_sdk_runtime")
        tool_calls = normalized.get("tool_calls")
        if isinstance(tool_calls, list):
            normalized["tool_calls"] = [self._normalize_tool_call_payload(item) for item in tool_calls]
        else:
            normalized["tool_calls"] = []
        if not normalized.get("final_text"):
            normalized["final_text"] = structured.get("summary") or normalized.get("summary") or ""
        return normalized

    def _normalize_tool_call_payload(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"tool_name": "unknown_tool", "arguments": {}, "output_summary": str(raw)[:240]}
        normalized = dict(raw)
        if "tool_name" not in normalized and "tool" in normalized:
            normalized["tool_name"] = normalized.pop("tool")
        if "arguments" not in normalized and "args" in normalized:
            normalized["arguments"] = normalized.pop("args")
        normalized.setdefault("tool_name", "unknown_tool")
        normalized.setdefault("arguments", {})
        return normalized

    def _tool_calls_from_run_items(self, run_items: list[Any]) -> list[AgentToolCallRecord]:
        pending: list[dict[str, Any]] = []
        for item in run_items:
            item_type = getattr(item, "type", "")
            if item_type == "tool_call_item":
                raw = getattr(item, "raw_item", None)
                call_id = self._raw_get(raw, "call_id") or self._raw_get(raw, "id")
                function_payload = self._raw_get(raw, "function") or {}
                tool_name = (
                    self._raw_get(raw, "name")
                    or self._raw_get(function_payload, "name")
                    or self._raw_get(raw, "tool_name")
                    or "unknown_tool"
                )
                raw_args = self._raw_get(raw, "arguments") or self._raw_get(function_payload, "arguments") or {}
                pending.append(
                    {
                        "tool_name": str(tool_name),
                        "arguments": self._parse_tool_arguments(raw_args),
                        "output_summary": None,
                        "call_id": call_id,
                    }
                )
            elif item_type == "tool_call_output_item":
                raw = getattr(item, "raw_item", None)
                call_id = self._raw_get(raw, "call_id") or self._raw_get(raw, "tool_call_id")
                output = getattr(item, "output", None)
                target = self._find_pending_tool_call(pending, call_id)
                if target is not None:
                    target["output_summary"] = self._summarize_tool_output(output)

        return [
            AgentToolCallRecord(
                tool_name=item["tool_name"],
                arguments=item["arguments"],
                output_summary=item.get("output_summary"),
            )
            for item in pending
        ]

    def _find_pending_tool_call(self, pending: list[dict[str, Any]], call_id: Any) -> dict[str, Any] | None:
        if call_id is not None:
            for item in reversed(pending):
                if item.get("call_id") == call_id and item.get("output_summary") is None:
                    return item
        for item in reversed(pending):
            if item.get("output_summary") is None:
                return item
        return pending[-1] if pending else None

    def _raw_get(self, raw: Any, key: str) -> Any:
        if isinstance(raw, dict):
            return raw.get(key)
        return getattr(raw, key, None)

    def _parse_tool_arguments(self, raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.debug("could not parse tool arguments as json: %s", raw_args)
        return {}

    def _summarize_tool_output(self, output: Any) -> str:
        if isinstance(output, dict):
            if "count" in output:
                examples = self._patient_examples_from_payload(output)
                suffix = f" examples={examples}" if examples else ""
                return f"count={output.get('count')}{suffix}"
            if "patient_ids" in output:
                examples = self._patient_examples_from_payload(output)
                suffix = f" examples={examples}" if examples else ""
                return f"patient_count={len(output.get('patient_ids') or [])}{suffix}"
            if "rows" in output:
                rows = output.get("rows") or []
                examples = self._patient_examples_from_rows(rows)
                suffix = f" examples={examples}" if examples else ""
                return f"rows={len(rows)}{suffix}"
        if isinstance(output, list):
            examples = self._doctor_examples_from_rows(output)
            suffix = f" examples={examples}" if examples else ""
            return f"rows={len(output)}{suffix}"
        text = str(output)
        return text[:240] + ("..." if len(text) > 240 else "")

    def _patient_examples_from_payload(self, payload: dict[str, Any]) -> str:
        patients = payload.get("patients")
        if isinstance(patients, list):
            return self._patient_examples_from_rows(patients)
        patient_names = payload.get("patient_names")
        patient_ids = payload.get("patient_ids") or []
        if isinstance(patient_names, dict):
            rows = [
                {
                    "patient_id": patient_id,
                    "patient_name": patient_names.get(patient_id) or patient_names.get(str(patient_id)),
                }
                for patient_id in patient_ids[:3]
            ]
            return self._patient_examples_from_rows(rows)
        return ""

    def _patient_examples_from_rows(self, rows: list[Any]) -> str:
        labels: list[str] = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            patient_id = row.get("patient_id")
            if patient_id is None:
                continue
            patient_name = row.get("patient_name")
            labels.append(f"{patient_name}（患者{patient_id}）" if patient_name else f"患者{patient_id}")
        return ", ".join(labels)

    def _doctor_examples_from_rows(self, rows: list[Any]) -> str:
        labels: list[str] = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            doctor_id = row.get("doctor_id")
            if doctor_id is None:
                continue
            doctor_name = row.get("doctor_name")
            labels.append(f"{doctor_name}（医生{doctor_id}）" if doctor_name else f"医生{doctor_id}")
        return ", ".join(labels)
