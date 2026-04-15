from __future__ import annotations

from agents import function_tool

from services import AnalyticsService

from .base import (
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
        """返回指定医生在某个时间窗内实际到训过的患者集合。"""
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
        """返回指定时间窗内存在计划的患者集合。"""
        return _list_patients_with_active_plans(
            doctor_id=doctor_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
        )

    def _set_diff(base_set_id: str, subtract_set_id: str) -> dict:
        return analytics_service.diff_patient_sets(
            base_set_id=base_set_id,
            subtract_set_id=subtract_set_id,
        ).model_dump(mode="json")

    @function_tool
    def set_diff(base_set_id: str, subtract_set_id: str) -> dict:
        """计算两个患者集合的差集。"""
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
        """返回患者最近一次到训时间及其简要上下文。"""
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
        """返回患者在指定窗口内的计划与到训状态。"""
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
        """按指定策略对患者列表排序。"""
        return _rank_patients(
            patient_ids=patient_ids,
            strategy=strategy,
            top_k=top_k,
        )

    return [
        ToolSpec(
            tool_name="list_patients_seen_by_doctor",
            description="返回指定医生在某个时间窗内实际到训过的患者集合。",
            input_model=DoctorWindowInput,
            output_schema="PatientSet JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_patients_seen_by_doctor,
            agent_tool=list_patients_seen_by_doctor,
            agent_handler=_list_patients_seen_by_doctor,
        ),
        ToolSpec(
            tool_name="list_patients_with_active_plans",
            description="返回指定时间窗内存在计划的患者集合。",
            input_model=DoctorWindowInput,
            output_schema="PatientSet JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_list_patients_with_active_plans,
            agent_tool=list_patients_with_active_plans,
            agent_handler=_list_patients_with_active_plans,
        ),
        ToolSpec(
            tool_name="set_diff",
            description="对两个患者集合做差集运算。",
            input_model=PatientSetDiffInput,
            output_schema="PatientSet JSON。",
            chain_scope="cross",
            can_affect_risk_score=False,
            direct_handler=_set_diff,
            agent_tool=set_diff,
            agent_handler=_set_diff,
        ),
        ToolSpec(
            tool_name="get_patient_last_visit",
            description="返回患者最近一次到训时间及关联简要信息。",
            input_model=LastVisitInput,
            output_schema="LastVisitInfo JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_patient_last_visit,
            agent_tool=get_patient_last_visit,
            agent_handler=_get_patient_last_visit,
        ),
        ToolSpec(
            tool_name="get_patient_plan_status",
            description="返回患者在指定窗口内是否有计划以及计划/到训摘要。",
            input_model=PatientPlanStatusInput,
            output_schema="PlanStatus JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_get_patient_plan_status,
            agent_tool=get_patient_plan_status,
            agent_handler=_get_patient_plan_status,
        ),
        ToolSpec(
            tool_name="rank_patients",
            description="按规则策略对患者列表排序。",
            input_model=RankPatientsInput,
            output_schema="RankedPatients JSON。",
            chain_scope="A",
            can_affect_risk_score=False,
            direct_handler=_rank_patients,
            agent_tool=rank_patients,
            agent_handler=_rank_patients,
        ),
    ]
