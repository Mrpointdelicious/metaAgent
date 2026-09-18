"""
创建日期：2026-09-03
文件功能：检查 Python 文件是否包含合法的标准文件头。
"""
import asyncio

from meta_agent.config import get_settings
from meta_agent.infrastructure.container import (
    create_task_planner,
)


async def main() -> None:
    settings = get_settings()

    planner = create_task_planner(
        settings
    )

    print(
        "Planner:",
        type(planner),
    )

    plan = await planner.plan(
        "分析一下最近一次训练情况。"
    )

    print(
        "Plan:",
        plan,
    )


if __name__ == "__main__":
    asyncio.run(main())