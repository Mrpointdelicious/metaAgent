"""
创建日期：2026-08-29
文件功能：支持通过 python -m meta_agent 启动 MetaAgent 服务。
"""

import uvicorn


def main() -> None:
    """启动 ASGI 服务。"""
    uvicorn.run("meta_agent.app:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
