"""
创建日期：2026-10-10
文件功能：运行流程（Flow）抽象与注册入口。

一条 Flow = 一个分支的完整处理流程。编排层（ApplicationService._dispatch）
按 domains.routing.route_from_inputs 得到 branch.name，再从这里注册的
application.flows 查表执行；未注册的分支明确返回不可用。

每个分支是一个文件夹 flows/<branch>/，约定：
  - 文件夹的 __init__.py 导出一个 Flow 类；
  - Flow 构造签名 Flow(application)，并实现 async def execute(self, ctx)；
  - 文件夹内可自由拆分为多个 .py 文件（prompts / steps / tools / ...）。

新增分支的步骤：
  1. 新建文件夹 flows/<branch>/，在 __init__.py 里定义 Flow 类；
  2. 在 domains/routing.py 的 route_from_inputs 里返回该 branch.name；
  3. 在 register_flows 的 branches 字典里加一行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext


class Flow(Protocol):
    """一条完整运行流程的接口。

    每个分支文件夹 __init__.py 里的 Flow 类需实现 execute；构造时统一
    接收 application（编排层 ApplicationService），供流程按需取用。
    """

    async def execute(self, ctx: RunContext) -> None:
        """在已加载会话内存、已进入并发/租约锁之后执行本分支流程。"""
        ...


def register_flows(application: Any) -> None:
    """把各分支 Flow 注册进 application.flows（由 container 装配时调用）。"""
    # 分支名 -> 分支包；每个包的 __init__.py 导出 Flow 类。
    from meta_agent.flows import consultation_room, default

    branches = {
        "default": default,
        "consultation_room": consultation_room,
    }
    for name, package in branches.items():
        application.flows[name] = package.Flow(application)
