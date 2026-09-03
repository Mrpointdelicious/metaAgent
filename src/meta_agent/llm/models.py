"""
创建日期：2026-08-29
文件功能：统一创建 MetaAgent 使用的 LangChain ChatModel。
"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from meta_agent.config import Settings


def create_planner_model(
    settings: Settings,
) -> BaseChatModel:
    """根据运行配置创建 Planner 使用的 LangChain ChatModel。"""

    api_key = settings.deepseek_api_key.get_secret_value()

    if not api_key:
        raise RuntimeError(
            "启用 LLM Planner 时必须配置 "
            "META_AGENT__DEEPSEEK_API_KEY"
        )

    try:
        model = init_chat_model(
            settings.planner_model,
            api_key=api_key,
            base_url=settings.deepseek_base_url,
            temperature=settings.planner_temperature,
            max_tokens=settings.planner_max_tokens,
            timeout=settings.planner_timeout_seconds,
            max_retries=settings.planner_max_retries,
        )
    except ImportError as exc:
        raise RuntimeError(
            "缺少 DeepSeek LangChain integration，"
            "请执行：uv sync --extra llm-deepseek --extra dev"
        ) from exc

    return model