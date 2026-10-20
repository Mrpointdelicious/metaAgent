"""
创建日期：2026-08-29
文件功能：提供本地开发环境的便捷启动入口，加载 src 目录中的 MetaAgent FastAPI 应用。
"""

import sys
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


if __name__ == "__main__":
    uvicorn.run(
        "meta_agent.app:app",
        host="127.0.0.1",
        port=8080,
        workers=1,
        loop="meta_agent.infrastructure.event_loop:loop_factory",
    )
