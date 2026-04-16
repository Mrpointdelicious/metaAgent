from __future__ import annotations

from agents import function_tool

from services import AnalyticsService

from .base import (
    DateWindowInput,
    DoctorWindowInput,
    LastVisitInput,
    PatientPlanStatusInput,
    PatientSetDiffInput,
    RankPatientsInput,
    ToolSpec,
)


def build_analytics_tools(analytics_service: AnalyticsService) -> list[ToolSpec]:
    def _list_patients_seen_by_doctor(
        doctor_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str = "attendance",
    ) -> dict:
        return analytics_service.list_patients_seen_by_doctor(
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
        ).model_dump(mode="json")

    @function_tool
    def list_patients_seen_by_doctor(
        doctor_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str = "attendance",
    ) -> dict:
        """Return the patients actually seen by a doctor inside a time window."""
        return _list_patients_seen_by_doctor(
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
        )

    def _list_patients_with_active_plans(
        doctor_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str = "attendance",
    ) -> dict:
        del source
        return analytics_service.list_patients_with_active_plans(
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
        ).model_dump(mode="json")

    @function_tool
    def list_patients_with_active_plans(
        doctor_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str = "attendance",
    ) -> dict:
        """Return the patients with plans inside a time window for a doctor."""
        return _list_patients_with_active_plans(
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
        )

    def _list_doctors_with_active_plans(
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        return [
            row.model_dump(mode="json")
            for row in analytics_service.list_doctors_with_active_plans(
                start_date=start_date,
                end_date=end_date,
            )
        ]

    @function_tool
    def list_doctors_with_active_plans(
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """Return doctor-level active plan aggregates inside a time window."""
        return _list_doctors_with_active_plans(
            start_date=start_date,
            end_date=end_date,
        )

    def _set_diff(base_set_id: str, subtract_set_id: str) -> dict:
        return analytics_service.diff_patient_sets(
            base_set_id=base_set_id,
            subtract_set_id=subtract_set_id,
        ).model_dump(mode="json")

    @function_tool
    def set_diff(base_set_id: str, subtract_set_id: str) -> dict:
        """Compute the set difference between two patient sets."""
        return _set_diff(
            base_set_id=base_set_id,
            subtract_set_id=subtract_set_id,
        )

    def _get_patient_last_visit(patient_id: int, doctor_id: int | None = None) -> dict:
        return analytics_service.get_patient_last_visit(
            patient_id=patient_id,
            doctor_id=doctor_id,
        ).model_dump(mode="json")

    @function_tool
    def get_patient_last_visit(patient_id: int, doctor_id: int | None = None) -> dict:
        """Return the latest visit for a patient."""
        return _get_patient_last_visit(
            patient_id=patient_id,
            doctor_id=doctor_id,
        )

    def _get_patient_plan_status(
        patient_id: int,
        doctor_id: int | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> dict:
        return analytics_service.get_patient_plan_status(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
        ).model_dump(mode="json")

    @function_tool
    def get_patient_plan_status(
        patient_id: int,
        doctor_id: int | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> dict:
        """Return plan status for a patient inside a time window."""
        return _get_patient_plan_status(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
        )

    def _rank_patients(
        patient_ids: list[int],
        strategy: str,
        top_k: int | None = None,
    ) -> dict:
        return analytics_service.rank_patients(
            patient_ids=patient_ids,
            strategy=strategy,
            top_k=top_k,
        ).model_dump(mode="json")

    @function_tool
    def rank_patients(
        patient_ids: list[int],
        strategy: str,
        top_k: int | None = None,
    ) -> dict:
        """Rank patients with a deterministic strategy."""
        return _rank_patients(
            patient_ids=patient_ids,
            strategy=strategy,
            top_k=top_k,
        )

    return [
        ToolSpec(
            tool_name="list_patients_seen_by_doctor",
            description="Return the patients actually seen by a doctor inside a window.",
            input_model=DoctorWindowInput,
            output_schema="PatientSet JSON",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_patients_seen_by_doctor,
            agent_tool=list_patients_seen_by_doctor,
            agent_handler=_list_patients_seen_by_doctor,
        ),
        ToolSpec(
            tool_name="list_patients_with_active_plans",
            description="Return the patients with plans for a doctor inside a window.",
            input_model=DoctorWindowInput,
            output_schema="PatientSet JSON",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_patients_with_active_plans,
            agent_tool=list_patients_with_active_plans,
            agent_handler=_list_patients_with_active_plans,
        ),
        ToolSpec(
            tool_name="list_doctors_with_active_plans",
            description="Return doctor-level active plan aggregates inside a window.",
            input_model=DateWindowInput,
            output_schema="DoctorAnalyticsResultRow[] JSON",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_doctors_with_active_plans,
            agent_tool=list_doctors_with_active_plans,
            agent_handler=_list_doctors_with_active_plans,
        ),
        ToolSpec(
            tool_name="set_diff",
            description="Compute the difference between two patient sets.",
            input_model=PatientSetDiffInput,
            output_schema="PatientSet JSON",
            chain_scope="cross",
            can_affect_risk_score=False,
            direct_handler=_set_diff,
            agent_tool=set_diff,
            agent_handler=_set_diff,
        ),
        ToolSpec(
            tool_name="get_patient_last_visit",
            description="Return the latest visit metadata for a patient.",
            input_model=LastVisitInput,
            output_schema="LastVisitInfo JSON",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_patient_last_visit,
            agent_tool=get_patient_last_visit,
            agent_handler=_get_patient_last_visit,
        ),
        ToolSpec(
            tool_name="get_patient_plan_status",
            description="Return whether a patient has plans in a window.",
            input_model=PatientPlanStatusInput,
            output_schema="PlanStatus JSON",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_patient_plan_status,
            agent_tool=get_patient_plan_status,
            agent_handler=_get_patient_plan_status,
        ),
        ToolSpec(
            tool_name="rank_patients",
            description="Rank patients with a deterministic strategy.",
            input_model=RankPatientsInput,
            output_schema="RankedPatients JSON",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_rank_patients,
            agent_tool=rank_patients,
            agent_handler=_rank_patients,
        ),
    ]
