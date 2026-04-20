from __future__ import annotations

from agents import function_tool

from services import ResultSetService

from .base import ResultSetInput, ResultSetWindowInput, ToolSpec


def build_result_set_tools(result_set_service: ResultSetService) -> list[ToolSpec]:
    # These tools return reusable collection semantics and therefore register
    # a new single active result set through ResultSetStore on success.
    def _identity_context():
        return result_set_service.repository.identity_context

    def _filter_result_set_by_training(result_set_id: str, days: int | None = None) -> dict:
        return result_set_service.filter_result_set_by_training(
            _identity_context(),
            result_set_id=result_set_id,
            days=days,
        )

    @function_tool
    def filter_result_set_by_training(result_set_id: str, days: int | None = None) -> dict:
        """Filter a registered patient result set to patients with training in a time window."""
        return _filter_result_set_by_training(result_set_id=result_set_id, days=days)

    def _filter_result_set_by_absence(result_set_id: str, days: int | None = None) -> dict:
        return result_set_service.filter_result_set_by_absence(
            _identity_context(),
            result_set_id=result_set_id,
            days=days,
        )

    @function_tool
    def filter_result_set_by_absence(result_set_id: str, days: int | None = None) -> dict:
        """Filter a registered patient result set to patients without training logs in a time window."""
        return _filter_result_set_by_absence(result_set_id=result_set_id, days=days)

    def _filter_result_set_by_plan_completion(result_set_id: str, days: int | None = None) -> dict:
        return result_set_service.filter_result_set_by_plan_completion(
            _identity_context(),
            result_set_id=result_set_id,
            days=days,
        )

    @function_tool
    def filter_result_set_by_plan_completion(result_set_id: str, days: int | None = None) -> dict:
        """Filter a registered patient result set to patients who completed plans in a time window."""
        return _filter_result_set_by_plan_completion(result_set_id=result_set_id, days=days)

    def _enrich_result_set_with_completion_time(result_set_id: str) -> dict:
        return result_set_service.enrich_result_set_with_completion_time(
            _identity_context(),
            result_set_id=result_set_id,
        )

    @function_tool
    def enrich_result_set_with_completion_time(result_set_id: str) -> dict:
        """Enrich a registered patient result set with latest plan completion time."""
        return _enrich_result_set_with_completion_time(result_set_id=result_set_id)

    return [
        ToolSpec(
            tool_name="filter_result_set_by_training",
            description="Filter an active patient result set by training activity inside a relative window.",
            input_model=ResultSetWindowInput,
            output_schema="ResultSet JSON with patient rows that had training logs",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_filter_result_set_by_training,
            agent_tool=filter_result_set_by_training,
            agent_handler=_filter_result_set_by_training,
        ),
        ToolSpec(
            tool_name="filter_result_set_by_absence",
            description="Filter an active patient result set by absence/no training logs inside a relative window.",
            input_model=ResultSetWindowInput,
            output_schema="ResultSet JSON with patient rows that lacked training logs",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_filter_result_set_by_absence,
            agent_tool=filter_result_set_by_absence,
            agent_handler=_filter_result_set_by_absence,
        ),
        ToolSpec(
            tool_name="filter_result_set_by_plan_completion",
            description="Filter an active patient result set by completed training plans inside a relative window.",
            input_model=ResultSetWindowInput,
            output_schema="ResultSet JSON with patient rows that completed plans",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_filter_result_set_by_plan_completion,
            agent_tool=filter_result_set_by_plan_completion,
            agent_handler=_filter_result_set_by_plan_completion,
        ),
        ToolSpec(
            tool_name="enrich_result_set_with_completion_time",
            description="Add completion_time to each patient row in an active result set.",
            input_model=ResultSetInput,
            output_schema="ResultSet JSON with completion_time fields",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_enrich_result_set_with_completion_time,
            agent_tool=enrich_result_set_with_completion_time,
            agent_handler=_enrich_result_set_with_completion_time,
        ),
    ]
