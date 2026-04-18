from __future__ import annotations

from agents import function_tool

from services import UserLookupService

from .base import LookupAccessibleUserInput, OptionalDaysInput, ToolSpec


def build_user_lookup_tools(user_lookup_service: UserLookupService) -> list[ToolSpec]:
    def _identity_context():
        return user_lookup_service.repository.identity_context

    def _lookup_accessible_user_name(user_id: int) -> dict:
        return user_lookup_service.lookup_accessible_user_name(
            _identity_context(),
            user_id=int(user_id),
        )

    @function_tool
    def lookup_accessible_user_name(user_id: int) -> dict:
        """Look up an accessible user's name within the current session identity scope."""
        return _lookup_accessible_user_name(user_id=user_id)

    def _list_my_patients(days: int | None = None) -> dict:
        return user_lookup_service.list_my_patients(
            _identity_context(),
            days=days,
        )

    @function_tool
    def list_my_patients(days: int | None = None) -> dict:
        """List patients related to the current doctor session."""
        return _list_my_patients(days=days)

    def _list_my_doctors(days: int | None = None) -> dict:
        return user_lookup_service.list_my_doctors(
            _identity_context(),
            days=days,
        )

    @function_tool
    def list_my_doctors(days: int | None = None) -> dict:
        """List doctors related to the current patient session."""
        return _list_my_doctors(days=days)

    return [
        ToolSpec(
            tool_name="lookup_accessible_user_name",
            description="Look up a user name only if the current identity can access that user.",
            input_model=LookupAccessibleUserInput,
            output_schema="JSON with user_id, user_name, user_role, is_accessible, found",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_lookup_accessible_user_name,
            agent_tool=lookup_accessible_user_name,
            agent_handler=_lookup_accessible_user_name,
        ),
        ToolSpec(
            tool_name="list_my_patients",
            description="Doctor-scoped roster: list patients related to the current doctor by visits or plans.",
            input_model=OptionalDaysInput,
            output_schema="JSON with count and rows containing patient_id and patient_name",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_my_patients,
            agent_tool=list_my_patients,
            agent_handler=_list_my_patients,
        ),
        ToolSpec(
            tool_name="list_my_doctors",
            description="Patient-scoped roster: list doctors related to the current patient by visits or plans.",
            input_model=OptionalDaysInput,
            output_schema="JSON with count and rows containing doctor_id and doctor_name",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_my_doctors,
            agent_tool=list_my_doctors,
            agent_handler=_list_my_doctors,
        ),
    ]
