"""
创建日期：2026-10-30
文件功能：适配分支业务能力并保留可信范围边界。
Read this run's authenticated conversation; never accept user IDs or URLs."""

from meta_agent.contracts import TaskResult


class ChatHistoryAdapter:
    async def execute(self, task, args, ctx):
        memory = await ctx.repository.conversation(ctx.scope.thread_id(ctx.record.conversation_id))
        turns = memory.turns[-6:]
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            "open_chat_history",
            ctx.record.request_id,
            {"turns": turns},
            "scoped-history-v1",
        )
        return TaskResult(
            task_id=task.task_id,
            evidence_ids=[evidence.evidence_id],
            message="已读取当前会话历史。" if turns else "当前会话暂无历史记录。",
            outputs={"turns": turns, "has_results": bool(turns)},
        )
