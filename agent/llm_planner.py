from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings

from .planner_prompts import build_planner_messages
from .schemas import LLMPlannedQuery, OrchestratorRequest, RoutedDecision


logger = logging.getLogger(__name__)


class LLMPlanner:
    def __init__(self, settings: Settings | None = None, *, max_steps: int = 8):
        self.settings = settings or get_settings()
        self.max_steps = max_steps

    def should_plan_with_llm(
        self,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
    ) -> bool:
        if routed_decision.final_intent != "open_analytics_query":
            return False

        subtype = routed_decision.final_subtype
        scope = routed_decision.final_scope
        question = request.raw_text or ""
        lowered = question.lower()

        if subtype == "absent_old_patients_recent_window" and not self._has_complex_window_signal(question):
            return False
        if subtype == "absent_from_baseline_window":
            return True
        if subtype == "doctors_with_active_plans":
            return True
        if scope == "doctor_aggregate":
            return True
        if subtype is None:
            return any(
                token in question or token in lowered
                for token in (
                    "compare",
                    "baseline",
                    "aggregate",
                    "which doctors",
                    "哪些医生",
                    "各医生",
                    "全院",
                    "统计",
                    "比较",
                )
            )
        return any(
            token in question or token in lowered
            for token in ("compare", "baseline", "排除", "统计", "比较", "基线", "前一阶段")
        )

    def plan(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        tool_catalog: list[dict[str, Any]],
        llm_config: ResolvedLLMConfig,
        mode: str,
    ) -> LLMPlannedQuery:
        if mode != "agents_sdk":
            raise RuntimeError("llm_planner.disabled: execution mode is not agents_sdk")
        if not llm_config.can_use_agents_sdk:
            raise RuntimeError("llm_planner.disabled: provider/model credentials are incomplete")
        if not llm_config.model:
            raise RuntimeError("llm_planner.disabled: no model configured")

        try:
            return self._call_llm(
                request=request,
                routed_decision=routed_decision,
                tool_catalog=tool_catalog,
                llm_config=llm_config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm planner failed: %s", exc)
            raise RuntimeError(f"llm_planner.failed:{type(exc).__name__}") from exc

    def _call_llm(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        tool_catalog: list[dict[str, Any]],
        llm_config: ResolvedLLMConfig,
    ) -> LLMPlannedQuery:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is not installed") from exc

        client_kwargs: dict[str, Any] = {"api_key": llm_config.api_key}
        if llm_config.base_url:
            client_kwargs["base_url"] = llm_config.base_url
        client = OpenAI(**client_kwargs)

        messages = build_planner_messages(
            request=request,
            routed_decision=routed_decision,
            tool_catalog=tool_catalog,
            max_steps=self.max_steps,
        )
        schema = LLMPlannedQuery.model_json_schema()
        try:
            response = client.chat.completions.create(
                model=llm_config.model,
                messages=messages,
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "LLMPlannedQuery",
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
        except Exception:
            response = client.chat.completions.create(
                model=llm_config.model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )

        content = response.choices[0].message.content if response.choices else ""
        if not content:
            raise ValueError("empty llm planner response")
        payload = json.loads(content)
        payload = self._normalize_planner_payload(payload, request=request, routed_decision=routed_decision)
        plan = LLMPlannedQuery.model_validate(payload)
        if plan.source != "llm_planner":
            raise ValueError("invalid planner source")
        return plan

    def _normalize_planner_payload(
        self,
        payload: Any,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("planner response must be a JSON object")
        normalized = dict(payload)
        if "steps" not in normalized:
            for nested_key in ("query_plan", "queryPlan", "plan", "LLMPlannedQuery", "planned_query", "plan_steps", "planned_steps"):
                nested = normalized.get(nested_key)
                if isinstance(nested, dict) and isinstance(nested.get("steps"), list):
                    merged = dict(nested)
                    for key, value in normalized.items():
                        if key not in {nested_key} and key not in merged:
                            merged[key] = value
                    normalized = merged
                    break
                if isinstance(nested, list):
                    normalized["steps"] = nested
                    break
        normalized.setdefault("normalized_question", request.raw_text or "")
        normalized.setdefault("subtype", routed_decision.final_subtype)
        normalized.setdefault("scope", routed_decision.final_scope)
        normalized.setdefault("source", "llm_planner")
        steps = normalized.get("steps")
        if isinstance(steps, dict):
            steps = list(steps.values())
            normalized["steps"] = steps
        if not isinstance(steps, list):
            raise ValueError(f"planner response missing steps list keys={sorted(normalized.keys())[:8]}")
        normalized_steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                raise ValueError(f"planner step {index} must be a JSON object")
            step = dict(raw_step)
            raw_step_id = step.get("step_id", f"step_{index}")
            if isinstance(raw_step_id, int):
                raw_step_id = f"step_{raw_step_id}"
            step["step_id"] = str(raw_step_id)
            if "tool_name" not in step and "tool" in step:
                step["tool_name"] = step.pop("tool")
            if "arguments" not in step and "args" in step:
                step["arguments"] = step.pop("args")
            step.setdefault("arguments", {})
            if "rationale" not in step and "reason" in step:
                step["rationale"] = step.pop("reason")
            step.setdefault("rationale", "")
            normalized_steps.append(step)
        normalized["steps"] = normalized_steps
        return normalized

    def _has_complex_window_signal(self, question: str) -> bool:
        lowered = question.lower()
        if any(token in question or token in lowered for token in ("baseline", "排除", "比较", "compare", "基线", "前一阶段")):
            return True
        return bool(
            re.search(r"\d+\s*[-到至]\s*\d+\s*(天|days?)", question, flags=re.IGNORECASE)
            or re.search(r"(past|last)\s*\d+\s*days?.*(exclude|except).*\d+\s*days?", lowered)
        )
