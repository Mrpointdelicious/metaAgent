import asyncio

from meta_agent.config import get_settings
from meta_agent.llm.models import create_planner_model


async def main() -> None:
    settings = get_settings()

    model = create_planner_model(settings)

    response = await model.ainvoke(
        "只回复以下六个字：模型连接成功"
    )

    print("response type:")
    print(type(response))

    print("\nresponse content:")
    print(response.content)


if __name__ == "__main__":
    asyncio.run(main())