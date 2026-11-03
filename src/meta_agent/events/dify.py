"""
创建日期：2026-09-12
文件功能：将原生终态事件适配为交互聊天文本、报表与 SSE，不重放场景动作。
"""

import json
from typing import Any

from meta_agent.contracts import RunRecord


class DifyEventAdapter:
    def message(self, record: RunRecord, *, reused: bool = False) -> dict[str, Any]:
        visible = {}
        event_keys = {}
        failures = {}
        artifacts = {}
        for event in record.events:
            if event.type in {"answer_part", "clarification"}:
                key = event.goal_id or event.event_id
                replaced_key = event_keys.get(event.payload.get("replaces"))
                if replaced_key is not None and replaced_key != key:
                    visible.pop(replaced_key, None)
                visible[key] = event.payload["text"]
                event_keys[event.event_id] = key
            elif event.type == "task_failed":
                failures[event.goal_id or event.event_id] = event.payload["text"]
            elif event.type == "artifact_ready":
                artifacts[event.payload["artifact_ref"]] = event.model_dump(mode="json")["payload"]
        text = list(visible.values())
        text.extend(
            value
            for goal, value in failures.items()
            if goal not in visible and (goal in record.goal_statuses or not visible)
        )
        answer = "\n\n".join(dict.fromkeys(value for value in text if value.strip()))
        for artifact in artifacts.values():
            if artifact["url"] not in answer:
                answer += ("\n\n" if answer else "") + f"![报告图片]({artifact['url']})"
        if not answer and record.status != "running":
            answer = "本次请求未形成可展示的回答，请查看运行状态或重新提问。"
        message_id = "message_" + record.run_id.removeprefix("run_")
        return {
            "event": "message",
            "task_id": record.run_id,
            "id": message_id,
            "message_id": message_id,
            "conversation_id": record.conversation_id,
            "mode": "chat",
            "answer": answer,
            "metadata": {
                "status": record.status,
                "request_id": record.request_id,
                "reused": reused,
                "tool_calls": record.metrics.get("tool_calls", 0),
                "llm_calls": record.metrics.get("llm_calls", 0),
                "knowledge_retrieval": record.metrics.get("knowledge_retrieval", []),
                "artifacts": list(artifacts.values()),
                "action_delivery": dict(record.action_delivery),
            },
            "created_at": int(record.created_at.timestamp()),
        }

    def frames(self, record: RunRecord) -> list[dict[str, Any]]:
        message = self.message(record)
        first = message
        if record.status in {"failed", "cancelled"}:
            failure = next((e for e in reversed(record.events) if e.type == "task_failed"), None)
            first = {
                "event": "error",
                "task_id": record.run_id,
                "message_id": message["message_id"],
                "conversation_id": record.conversation_id,
                "status": 500,
                "code": failure.payload["code"] if failure else "request_" + record.status,
                "message": message["answer"],
            }
        return [
            first,
            {
                key: value
                for key, value in message.items()
                if key not in {"event", "answer", "mode", "id"}
            }
            | {"event": "message_end"},
        ]

    def encode(self, frame: dict[str, Any]) -> str:
        return "data: " + json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n\n"
