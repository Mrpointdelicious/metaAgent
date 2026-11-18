"""
创建日期：2026-11-16
文件功能：把会话记忆整理成供 LLM 使用的对话文本（纪要总结类流程共用）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

# 只保留最近若干轮，且总长截断，避免长会话把模型上下文顶满。
TRANSCRIPT_MAX_TURNS = 20
TRANSCRIPT_MAX_CHARS = 12000


def build_transcript(ctx: RunContext) -> str:
    """汇总最近若干轮对话与本轮输入，返回拼接后的对话文本。"""
    lines: list[str] = []
    for turn in ctx.memory.turns[-TRANSCRIPT_MAX_TURNS:]:
        user_text = str(turn.get("user") or "").strip()
        assistant_text = str(turn.get("assistant") or "").strip()
        if user_text:
            lines.append("用户：" + user_text)
        if assistant_text:
            lines.append("助手：" + assistant_text)
    lines.append("用户（本轮）：" + ctx.query)
    transcript = "\n".join(lines)
    if len(transcript) > TRANSCRIPT_MAX_CHARS:
        transcript = transcript[-TRANSCRIPT_MAX_CHARS:]
    return transcript
