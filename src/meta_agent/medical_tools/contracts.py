"""
创建日期：2026-11-17
文件功能：定义稳定工具调用接口及现有领域封套类型，不改变业务校验和数据格式。

来源：metaAgent-dev 的 `medical_tools/contracts.py`（2026-11-15）。泛型写法沿用 PEP 695，
项目要求 Python >= 3.12。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from meta_agent.orchestration.identity import TrustedScope

CONTRACT_VERSION = "medical-tools-v1"


class SceneArguments(TypedDict, total=False):
    target: str
    spaceId: str | None
    expectedSceneVersion: int | None
    pos: str | None
    currentPos: str | None


class DoctorArguments(TypedDict, total=False):
    # 房间外挂号：spaceId 必须传患者当前空间（/api/users 严格按空间过滤）。
    # 不设 inviterPhone——那是 /api/users/all 房间内在场口径的参数，本接口不认。
    spaceId: str | None


class PatientArguments(TypedDict, total=False):
    # 领域参数仍由各工具现有 schema 管理；这里记录跨工具复用的稳定引用。
    session_ref: str | None
    plan_ref: str | None
    assessment_ref: str | None
    expected_source_snapshot: str | dict[str, Any] | None
    page_number: int
    page_size: int


class SceneCommand(TypedDict):
    order: int
    sourceText: str
    status: Literal["matched", "ambiguous", "unmatched"]
    result: NotRequired[str | None]
    matchName: NotRequired[str]
    intentType: NotRequired[str]
    score: NotRequired[float]
    message: NotRequired[str]
    candidates: NotRequired[list[dict[str, Any]]]


class SceneData(TypedDict):
    spaceId: str
    version: int
    commands: list[SceneCommand]


class SceneResponse(TypedDict):
    status: int
    data: SceneData


class DoctorData(TypedDict):
    count: int
    doctorNames: str
    doctorIds: str
    doctorPhones: str
    command: str
    result: NotRequired[str]


class DoctorResponse(TypedDict):
    status: int
    message: NotRequired[str]
    data: DoctorData


class HospitalEntry(TypedDict):
    hospital_id: int
    hospital_name: str


class DirectoryResponse(TypedDict):
    directory: list[HospitalEntry]


class ClinicalResponse(TypedDict):
    tool_name: str
    contract_version: str
    request_id: str
    status: Literal["success", "available", "partial", "unavailable", "failed"]
    data: dict[str, Any] | None
    meta: dict[str, Any]
    query_scope: NotRequired[dict[str, Any]]
    patient_message: NotRequired[str | None]
    reason_codes: NotRequired[list[str]]


@dataclass(frozen=True)
class ToolContext:
    """系统注入的运行依赖；不作为模型参数，不写入 checkpoint 或日志。"""

    scope: TrustedScope = field(repr=False)
    request_id: str = ""
    conversation_id: str = ""


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return dict(self.arguments)


@dataclass(frozen=True)
class ToolResult[Payload: (SceneResponse, DoctorResponse, DirectoryResponse, ClinicalResponse)]:
    """进程内统一封套；payload 原样交还领域层，沿用已有校验与证据指针。"""

    tool_name: str
    payload: Payload
    contract_version: Literal["medical-tools-v1"] = CONTRACT_VERSION

    def domain_payload(self) -> Payload:
        return self.payload


class ToolAdapter(Protocol):
    async def execute(self, request: ToolRequest, context: ToolContext | None) -> ToolResult: ...
