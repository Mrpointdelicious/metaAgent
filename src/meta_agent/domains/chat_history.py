"""
创建日期：2026-10-16
文件功能：打开历史对话记录——按当前 user/space 拼出聊天页网址。

对应 Dify 代码节点 main(user_id, space_id)：
  user_id 来自输入 user（可信网关解析出的 end_user_id），space_id 来自输入
  spaceid（scope.space_id）。纯本地拼 URL，不依赖上游接口；成功时输出
  legacy_web = "[web]" + web_url，供前端打开聊天页。

日志约定：只记录不含个人数据的诊断字段（run_id/event_type/action/error 等），
不打印完整 user_id / space_id / URL。
"""

import logging
from typing import Any
from urllib.parse import quote

from meta_agent.application.context import RunContext
from meta_agent.contracts import TaskResult, TaskSpec

logger = logging.getLogger(__name__)

TOOL_KIND = "chat_history"
CONTRACT_VERSION = "chat-history-v1"
ACTION = "open_chat_history"
CHAT_URL = "http://192.168.8.160:9090/chatPage/"

# 打开/查看历史对话记录的关键词，用于确定性识别（与场景指令识别区分开）。
_CHAT_HISTORY_MARKERS = ("对话记录", "聊天记录", "对话历史", "历史对话", "聊天历史")


def is_chat_history_query(query: str) -> bool:
    """确定性判定：用户话术是否要打开/查看历史对话记录。"""
    q = (query or "").strip()
    if not q:
        return False
    return any(m in q for m in _CHAT_HISTORY_MARKERS)


def build(user_id: str, space_id: str) -> dict[str, str]:
    """拼聊天页网址，输出与 Dify 代码节点 main() 一致的契约。"""
    uid = str(user_id or "").strip()
    sid = str(space_id or "").strip()

    if not uid:
        return {
            "tool_kind": TOOL_KIND,
            "success": "false",
            "status": "failed",
            "action": ACTION,
            "web_url": "",
            "legacy_web": "",
            "patient_message": "当前无法确定用户身份，不能打开历史对话记录。",
            "error": "user_id required",
        }
    if not sid:
        return {
            "tool_kind": TOOL_KIND,
            "success": "false",
            "status": "failed",
            "action": ACTION,
            "web_url": "",
            "legacy_web": "",
            "patient_message": "当前无法确定所在空间，不能打开历史对话记录。",
            "error": "space_id required",
        }

    web_url = CHAT_URL + "?user=" + quote(uid, safe="") + "&spaceId=" + quote(sid, safe="")
    return {
        "tool_kind": TOOL_KIND,
        "success": "true",
        "status": "success",
        "action": ACTION,
        "web_url": web_url,
        "legacy_web": "[web]" + web_url,
        "patient_message": "已为您打开历史对话记录。",
        "error": "",
    }


class ChatHistoryAdapter:
    """把聊天页网址拼好落证据库，并投影为 TaskResult 供 agent / 后处理层消费。"""

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        del args
        # user_id 来自输入 user（end_user_id），space_id 来自输入 spaceid（scope.space_id）。
        out = build(ctx.scope.end_user_id, ctx.scope.space_id or "")
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash, "open_chat_history", ctx.record.request_id, out, CONTRACT_VERSION
        )
        success = out.get("success") == "true"
        logger.info(
            "chat history url built",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "chat_history",
                "fields": {
                    "tool": ACTION,
                    "success": success,
                    "error": out.get("error") or "",
                },
            },
        )
        return TaskResult(
            task_id=task.task_id,
            status="succeeded" if success else "unavailable",
            message=out.get("patient_message") or "",
            evidence_ids=[evidence.evidence_id],
            outputs={
                "tool_name": ACTION,
                # 后处理层用它拼最终答复：[web]<url>（失败时为空）。
                "command": out.get("legacy_web") or "",
                "web_url": out.get("web_url") or "",
                "action": out.get("action") or "",
                "error": out.get("error") or "",
                "contract_version": CONTRACT_VERSION,
            },
        )
