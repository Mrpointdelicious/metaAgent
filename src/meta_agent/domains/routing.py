"""
创建日期：2026-10-10
文件功能：根据可信网关 inputs 判定运行分支。

这是确定性的业务路由：规则集中维护在此、纯函数可单测，返回稳定分支标识，
不包含任何执行逻辑（执行逻辑在 flows/ 下按分支实现）。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Branch:
    """运行分支的稳定标识。新增分支时在此返回新 name，并在 flows/ 里注册对应 Flow。"""

    name: str


def route_from_inputs(raw_inputs: dict[str, Any]) -> Branch:
    """把前端 inputs 映射到分支名。

    - pos 包含连续的「问诊房间」→ consultation_room
    - 其余 → default（分支 B，即当前的 agent 默认流程）

    规则变复杂时只改这里，别散落到业务代码。
    """
    pos = raw_inputs.get("pos", "")
    if not isinstance(pos, str):
        raise ValueError("pos 必须是字符串")
    if "问诊房间" in pos.strip():
        return Branch(name="consultation_room")
    return Branch(name="default")
