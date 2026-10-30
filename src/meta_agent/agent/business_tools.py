"""
创建日期：2026-10-30
文件功能：适配分支业务能力并保留可信范围边界。
Business tool registration outside patient runtime and contracts."""

from pydantic import Field

from meta_agent.agent.schemas import EmptyArgs, SceneArgs
from meta_agent.contracts import DomainError, StrictModel
from meta_agent.domains.chat_history import ChatHistoryAdapter
from meta_agent.domains.doctors import DoctorAdapter
from meta_agent.domains.hospital import HospitalAdapter


class HospitalDirectoryArgs(StrictModel):
    name: str = Field(default="", max_length=200)


class DoctorSelectionArgs(StrictModel):
    selection: str = Field(min_length=1, max_length=200)


BUSINESS_TOOLS = {
    "query_hospital_directory": (
        HospitalDirectoryArgs,
        "查询可信医院目录，可按名称过滤。仅运营人员可用。",
    ),
    "search_online_doctors": (EmptyArgs, "查询当前空间在线医生，不联系医生。"),
    "resolve_doctor_selection": (DoctorSelectionArgs, "按本轮原话从已查询名单选择医生，不挂号。"),
    "request_registration": (EmptyArgs, "挂号授权接口未接入时明确返回不可用，不联系医生。"),
    "resolve_scene_instruction": (
        SceneArgs,
        "仅解析本轮原话的场景指令，返回匹配状态，不交付动作。",
    ),
    "open_chat_history": (EmptyArgs, "读取当前可信用户在当前会话中的历史，不接受用户ID或链接。"),
}


def business_allowed(ctx, name):
    if name == "query_hospital_directory":
        return ctx.scope.role == "operator"
    if name in {"search_online_doctors", "resolve_doctor_selection", "request_registration"}:
        return ctx.settings.doctors_enabled and bool(ctx.scope.space_id)
    if name == "resolve_scene_instruction":
        return ctx.settings.scene_enabled and bool(ctx.scope.space_id)
    return name == "open_chat_history"


async def execute_business(task, args, ctx, name):
    if name == "query_hospital_directory":
        return await HospitalAdapter().directory(task, args, ctx)
    if name == "search_online_doctors":
        return await DoctorAdapter().execute(task, args, ctx)
    if name == "resolve_doctor_selection":
        return await DoctorAdapter().select(task, args, ctx)
    if name == "request_registration":
        raise DomainError(
            "registration_authorization_unavailable",
            "挂号授权服务尚未接入，不能代为完成挂号。",
            outcome="unsupported",
        )
    if name == "open_chat_history":
        return await ChatHistoryAdapter().execute(task, args, ctx)
    from meta_agent.domains.scene_instruction import SceneInstructionAdapter

    return await SceneInstructionAdapter().execute(task, args, ctx, dispatch=False)
