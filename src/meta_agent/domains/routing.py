"""
创建日期：2026-10-10
文件功能：根据可信网关 inputs 判定运行分支。

这是确定性的业务路由：规则集中维护在此、纯函数可单测，返回稳定分支标识，
不包含任何执行逻辑（执行逻辑在 flows/ 下按分支实现）。
"""

from dataclasses import dataclass
from typing import Any, Literal

from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import user_role_from_inputs
from meta_agent.roles import ROLE_AGENTS

# 会话类型 = 运行分支名；新增分支时同步这里与 flows 注册表。
ConversationKind = Literal[
    "default",
    "consultation_room",
    "ai_summary",
    "ai_medical_order",
    "ai_prescription",
    "ai_realtime_analysis",
    "ai_consultation",
]


@dataclass(frozen=True, slots=True)
class Branch:
    """运行分支的稳定标识。新增分支时在此返回新 name，并在 flows/ 里注册对应 Flow。"""

    name: str


# 临床上下文分支：这些分支的会话不信任 inputs 里的患者查找参数
# （patientphone 等只作运行时参数，不建立患者 v2 身份），见 api/routes.py。
CLINICAL_BRANCHES = frozenset(
    {
        "consultation_room",
        "ai_consultation",
        "ai_medical_order",
        "ai_prescription",
        "ai_realtime_analysis",
        "ai_summary",
    }
)


def is_consultation_position(pos: str) -> bool:
    if not isinstance(pos, str):
        raise ValueError("pos 必须是字符串")
    return "问诊" in pos and "房间" in pos


def route_from_inputs(
    raw_inputs: dict[str, Any], forced: str = "", *, role: str | None = None
) -> Branch:
    """把前端 inputs 映射到分支名。

    - 功能令牌优先选择已绑定流程，并校验活动角色是否允许该流程。
    - 只有角色方案允许问诊，且 pos 同时含「问诊」和「房间」，才选择 consultation_room。
    - 其余 → default（分支 B，即当前的 agent 默认流程）

    「问诊中心」是患者大厅，不含「房间」，仍留在 default 分支。
    规则变复杂时只改这里，别散落到业务代码。
    """
    room = is_consultation_position(raw_inputs.get("pos", ""))
    if forced:
        active_role = role
        if active_role is None and {"role", "identity"}.intersection(raw_inputs):
            active_role = user_role_from_inputs(raw_inputs)
        if active_role is not None and forced not in ROLE_AGENTS.for_role(active_role).flows:
            raise DomainError(
                "role_access_denied", "当前角色无权使用此功能流程。", outcome="unsupported"
            )
        return Branch(name=forced)
    active_role = role if role is not None else user_role_from_inputs(raw_inputs)
    if room and "consultation_room" in ROLE_AGENTS.for_role(active_role).flows:
        return Branch(name="consultation_room")
    return Branch(name="default")
