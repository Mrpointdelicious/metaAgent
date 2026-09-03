"""
创建日期：2026-09-03
文件功能：提供与本地 .env 隔离的 MetaAgent 测试配置。
"""

from pydantic_settings import SettingsConfigDict

from meta_agent.config import Settings


class TestSettings(Settings):
    """测试专用 Settings，不读取项目根目录 .env。"""

    model_config = SettingsConfigDict(
        **{
            **Settings.model_config,
            "env_file": None,
        }
    )