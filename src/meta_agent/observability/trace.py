"""
创建日期：2026-10-29
文件功能：当前请求 run_id 的上下文变量。

问诊房间等分支流程里，底层 HTTP 助手（_login/_get 等）拿不到 RunContext，
无法在日志里带 run_id。这里用一个 ContextVar 把当前 run_id 透传下去，
使这些底层接口调用日志也能按 run_id 关联到具体请求。
"""

from contextvars import ContextVar

CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("metaagent_run_id", default=None)
