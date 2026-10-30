"""
创建日期：2026-08-29
文件功能：生成 Dify chat-messages 风格的 SSE 事件与阻塞响应，作为 drop-in 兼容层。

流式返回对齐 Dify chatflow 的生命周期事件：workflow_started →（每工具）node_started /
node_finished → message → workflow_finished → message_end。阻塞返回仍只给最终 message。
"""

import json
import re
import time
from typing import Any

from meta_agent.contracts import OutboundEvent, RunRecord, identifier


def _now() -> int:
    return int(time.time())


def _dify_status(outcome: str | None) -> str:
    """内部 OutcomeStatus -> Dify node/workflow 状态。"""
    return "succeeded" if outcome in {"succeeded", "partial"} else "failed"


class DifyEventFactory:
    """生成 Dify chat-messages 兼容事件。"""

    def __init__(self, task_id: str, conversation_id: str) -> None:
        self.task_id = task_id
        self.conversation_id = conversation_id
        # Dify 中 message_id（消息）与 task_id（工作流运行）是两个不同标识。
        self.message_id = identifier("msg")

    def _envelope(self, event: str) -> dict[str, Any]:
        return {
            "event": event,
            "task_id": self.task_id,
            "id": self.message_id,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "created_at": _now(),
        }

    def message(self, answer: str) -> dict[str, Any]:
        body = self._envelope("message")
        body["mode"] = "chat"
        body["answer"] = answer
        return body

    def message_end(self, metadata: dict[str, Any]) -> dict[str, Any]:
        body = self._envelope("message_end")
        body["metadata"] = metadata
        return body

    def workflow_started(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        inputs = inputs or {}
        workflow_id = str(
            inputs.get("sys.workflow_id") or inputs.get("workflow_id") or "metaagent"
        )
        body = self._envelope("workflow_started")
        # Dify 中 workflow_run_id 与 task_id 同值，指向本次工作流运行；前端据此回显入参。
        body["workflow_run_id"] = self.task_id
        body["data"] = {
            "id": self.task_id,
            "workflow_id": workflow_id,
            "sequence_number": 1,
            "inputs": inputs,
            "created_at": _now(),
        }
        return body

    def node_started(
        self, node_id: str, title: str, index: int, node_type: str = "tool"
    ) -> dict[str, Any]:
        body = self._envelope("node_started")
        body["data"] = {
            "id": f"{self.task_id}:{node_id}",
            "node_id": node_id,
            "node_type": node_type,
            "title": title,
            "index": index,
            "predecessor_node_id": None,
            "inputs": None,
            "created_at": _now(),
        }
        return body

    def node_finished(
        self,
        node_id: str,
        title: str,
        index: int,
        status: str,
        node_type: str = "tool",
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        body = self._envelope("node_finished")
        body["data"] = {
            "id": f"{self.task_id}:{node_id}",
            "node_id": node_id,
            "node_type": node_type,
            "title": title,
            "index": index,
            "predecessor_node_id": None,
            "inputs": None,
            "process_data": None,
            "outputs": outputs,
            "status": status,
            "error": error,
            "elapsed_time": 0.0,
            "execution_metadata": {},
            "created_at": _now(),
            "finished_at": _now(),
        }
        return body

    def workflow_finished(
        self,
        status: str,
        outputs: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        body = self._envelope("workflow_finished")
        body["data"] = {
            "id": self.task_id,
            "workflow_id": "metaagent",
            "status": status,
            "outputs": outputs,
            "error": error,
            "elapsed_time": 0.0,
            "total_tokens": 0,
            "total_steps": 0,
            "created_at": _now(),
            "finished_at": _now(),
        }
        return body

    def error(
        self, message: str, code: str = "agent_execution_failed", status: int = 500
    ) -> dict[str, Any]:
        return {
            "event": "error",
            "task_id": self.task_id,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "status": status,
            "code": code,
            "message": message,
        }


def empty_metadata() -> dict[str, Any]:
    """Dify message_end 的最小 metadata。"""
    return {"usage": {}, "retriever_resources": []}


def encode_sse(event: dict[str, Any]) -> str:
    """将事件编码成 SSE data 帧。"""
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


def encode_ping() -> str:
    """生成与 Dify 流式接口兼容的保活帧。"""
    return "event: ping\n\n"


_SEGMENT_RE = re.compile(r"\[[^\]]*\][^\[]*")


def split_answer_segments(text: str) -> list[str]:
    """把「[标记]值」串联的答复拆成多个片段，每个片段对应一个 message。

    "[timestamp=150.26][ScenePoint:]5[ClickPoint:]1[Answer:]好的。" ->
        ["[timestamp=150.26]", "[ScenePoint:]5", "[ClickPoint:]1", "[Answer:]好的。"]
    结构化 JSON 或不含 '[' 的纯文本保持单条，避免拆坏结构化事件。
    """
    if not text or text.lstrip().startswith("{") or "[" not in text:
        return [text]
    segments = _SEGMENT_RE.findall(text)
    if not segments:
        return [text]
    # 只有片段能完整拼回原文时才拆分，否则（前导/夹缝裸文本）保留原文。
    if "".join(segments) != text:
        return [text]
    return segments


class DifyEventAdapter:
    """把内部 OutboundEvent 转成 Dify chat-messages 的 SSE 帧；无关事件返回空串。

    内部事件流只有 accepted / progress / answer_part / task_failed / completed 等，
    没有显式的 node 开始结束。这里把带 task_id 的 progress（agent 模式的工具调用）
    当作一个 node，串行工具由下一个 progress 或终态事件触发 node_finished。
    """

    def __init__(self, record: RunRecord) -> None:
        self.record = record
        self.factory = DifyEventFactory(record.run_id, record.conversation_id)
        self._workflow_started = False
        self._workflow_finished = False
        self._nodes: dict[str, dict[str, Any]] = {}
        self._next_index = 0
        self._answer_texts: list[str] = []

    def _node_meta(self, task_id: str) -> tuple[str, str]:
        result = self.record.results.get(task_id)
        if result is not None:
            title = str(result.outputs.get("tool_name") or task_id)
            return title, "tool"
        if self.record.plan is not None:
            for task in self.record.plan.tasks:
                if task.task_id == task_id:
                    return task.capability, "tool"
        return task_id, "tool"

    def _finish_node(self, task_id: str) -> str:
        node = self._nodes.get(task_id)
        if node is None:
            return ""
        title, node_type = self._node_meta(task_id)
        result = self.record.results.get(task_id)
        outcome = result.status if result is not None else "failed"
        error = None
        if result is not None and result.status not in {"succeeded", "partial"}:
            error = result.code or "failed"
        self._nodes.pop(task_id, None)
        return encode_sse(
            self.factory.node_finished(
                task_id,
                title,
                node["index"],
                _dify_status(outcome),
                node_type=node_type,
                outputs=result.outputs if result is not None else None,
                error=error,
            )
        )

    def _finish_open_nodes(self) -> str:
        return "".join(self._finish_node(tid) for tid in list(self._nodes.keys()))

    def encode(self, event: OutboundEvent) -> str:
        if event.type == "accepted":
            if self._workflow_started:
                return ""
            self._workflow_started = True
            return encode_sse(self.factory.workflow_started(self.record.raw_inputs))
        if event.type == "progress" and event.task_id:
            task_id = event.task_id
            if task_id in self._nodes:
                return ""
            prefix = self._finish_open_nodes()
            title, node_type = self._node_meta(task_id)
            index = self._next_index
            self._next_index += 1
            self._nodes[task_id] = {"index": index}
            return prefix + encode_sse(self.factory.node_started(task_id, title, index, node_type))
        if event.type in {"answer_part", "clarification"}:
            self._answer_texts.append(event.payload.get("text", ""))
            prefix = self._finish_open_nodes()
            segments = split_answer_segments(event.payload["text"])
            return prefix + "".join(
                encode_sse(self.factory.message(segment)) for segment in segments
            )
        if event.type == "task_failed":
            prefix = self._finish_open_nodes()
            return prefix + encode_sse(
                self.factory.error(
                    event.payload.get("text", ""),
                    code=event.payload.get("code", "agent_execution_failed"),
                )
            )
        if event.type == "completed":
            prefix = self._finish_open_nodes()
            if not self._workflow_finished:
                self._workflow_finished = True
                outcome = event.payload.get("outcome", "succeeded")
                answer = "\n".join(self._answer_texts)
                prefix += encode_sse(
                    self.factory.workflow_finished(
                        _dify_status(outcome), outputs={"answer": answer}
                    )
                )
            return prefix + encode_sse(self.factory.message_end(empty_metadata()))
        return ""
