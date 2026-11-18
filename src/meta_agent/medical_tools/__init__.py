"""
创建日期：2026-11-17
文件功能：医疗工具统一入口（契约 / 注册表 / 网关 / 适配器）。

分层（沿用 metaAgent-dev 的 medical_tools 设计，以当前主线代码为准）：

    LangGraph / Flow / RunContext
        ↓  RunContext.call → MedicalToolGateway.post
    Registry（工具名、参数、功能开关、提供方）
        ├── AIWebApiAdapter        → .NET AI_WebApi（患者、康复、量表、报表、医院运营）
        ├── platform/scene.py      → 中台点位库（场景指令，本地匹配）
        └── platform/doctors.py    → 中台在线医生（房间外挂号名单）

与 metaAgent-dev 的差异：场景/医生实现来自当前主线（原 `domains/scene_local.py`、
`domains/doctors_local.py` 原样迁入并保留 /api/users 口径与 [DoctorRecommend:] 链），
不是 dev 快照里的 /api/users/all 版本。
"""

from meta_agent.medical_tools.contracts import (
    CONTRACT_VERSION,
    DoctorArguments,
    DoctorResponse,
    SceneArguments,
    SceneResponse,
    ToolAdapter,
    ToolContext,
    ToolRequest,
    ToolResult,
)
from meta_agent.medical_tools.errors import ToolError
from meta_agent.medical_tools.gateway import MedicalToolGateway
from meta_agent.medical_tools.registry import ToolSpec, registry, require_enabled

__all__ = [
    "CONTRACT_VERSION",
    "DoctorArguments",
    "DoctorResponse",
    "MedicalToolGateway",
    "SceneArguments",
    "SceneResponse",
    "ToolAdapter",
    "ToolContext",
    "ToolError",
    "ToolRequest",
    "ToolResult",
    "ToolSpec",
    "registry",
    "require_enabled",
]
