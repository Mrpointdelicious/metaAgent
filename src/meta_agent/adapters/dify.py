"""
创建日期：2026-08-29
文件功能：生成 Dify 风格 SSE 事件和阻塞响应，作为 0.3.x Workflow 迁移兼容层。
"""

import json
import time
from typing import Any


class DifyEventFactory:
    """生成包含稳定运行标识的 Dify 兼容事件。"""

    def __init__(
        self,
        task_id: str,
        workflow_run_id: str,
        conversation_id: str,
    ) -> None:
        self.task_id = task_id
        self.workflow_run_id = workflow_run_id
        self.conversation_id = conversation_id

    def workflow_started(self) -> dict[str, Any]:
        return {
            "event": "workflow_started",
            "task_id": self.task_id,
            "workflow_run_id": self.workflow_run_id,
            "data": {"status": "running"},
        }

    def node_finished(self, node_name: str) -> dict[str, Any]:
        return {
            "event": "node_finished",
            "task_id": self.task_id,
            "workflow_run_id": self.workflow_run_id,
            "data": {"title": node_name, "status": "succeeded"},
        }

    def message(self, answer: str) -> dict[str, Any]:
        return {
            "event": "message",
            "task_id": self.task_id,
            "message_id": self.task_id,
            "conversation_id": self.conversation_id,
            "answer": answer,
            "created_at": int(time.time()),
        }

    def message_end(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "event": "message_end",
            "task_id": self.task_id,
            "message_id": self.task_id,
            "conversation_id": self.conversation_id,
            "metadata": metadata,
        }

    def workflow_finished(
        self,
        status: str,
        outputs: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event": "workflow_finished",
            "task_id": self.task_id,
            "workflow_run_id": self.workflow_run_id,
            "data": {"status": status, "outputs": outputs, "error": error},
        }

    def error(self, message: str) -> dict[str, Any]:
        return {
            "event": "error",
            "task_id": self.task_id,
            "status": 500,
            "code": "agent_execution_failed",
            "message": message,
        }


def encode_sse(event: dict[str, Any]) -> str:
    """将事件编码成 SSE data 帧。"""
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


def encode_ping() -> str:
    """生成与 Dify 流式接口兼容的保活帧。"""
    return "event: ping\n\n"
