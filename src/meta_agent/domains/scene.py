"""
创建日期：2026-09-08
文件功能：对齐新版commands数组，分别处理匹配/歧义/缺失及逐项动作交付。
"""

import re
from typing import Any

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec, fingerprint
from meta_agent.domains.rehab import list_field, object_field
from meta_agent.infrastructure.repository import resolve_pointer

COMMAND = re.compile(r"^\[(Telepor|ScenePoint|ClickPoint|GamePoint):\][0-9]+$")


def source_text(text: str) -> str:
    return re.sub(r"[\s，,。.;；!?！？]", "", text)


def numeric_envelope(body: dict[str, Any]) -> dict[str, Any]:
    status = body.get("status")
    if type(status) is not int:
        raise DomainError("invalid_contract", "场景/医生工具封套类型无效。")
    if status != 200:
        raise DomainError(f"tool_status_{status}", "工具业务处理失败。")
    return object_field(body, "data")


class SceneAdapter:
    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        if task.capability == "scene.dispatch":
            return await self.dispatch(task, args["action_ref"], ctx)
        body = await ctx.call("navigate_scene", {"target": args["target"], "spaceId": ctx.scope.space_id})
        data = numeric_envelope(body)
        commands = list_field(data, "commands")
        if data.get("spaceId") != ctx.scope.space_id or type(data.get("version")) is not int or data["version"] < 0:
            raise DomainError("scene_scope_mismatch", "场景目录与当前空间不一致。")
        if len(commands) != len(task.goal_ids):
            raise DomainError("command_alignment", "指令片段未能逐项对齐，请明确各个动作。", outcome="clarification")
        if any(type(c.get("order")) is not int or c["order"] != i+1 for i, c in enumerate(commands)):
            raise DomainError("command_order", "指令结果顺序无效。")
        for command, gid in zip(commands, task.goal_ids, strict=True):
            if source_text(str(command.get("sourceText") or "")) != source_text(ctx.goals[gid].query_span):
                raise DomainError("command_alignment", "工具结果包含未对应到原话的动作。", outcome="clarification")
            if command.get("status") not in {"matched", "ambiguous", "unmatched"}:
                raise DomainError("invalid_command_status", "指令匹配状态无效。")
            if command["status"] == "matched" and not COMMAND.fullmatch(str(command.get("result") or "")):
                raise DomainError("invalid_command", "动作编码不符合已注册的目录协议。")
            if command["status"] != "matched" and command.get("result") is not None:
                raise DomainError("ambiguous_executable", "不明确的指令不能携带可执行编码。")
        evidence = await ctx.repository.save_evidence(ctx.scope.scope_hash, "navigate_scene",
            ctx.record.request_id, body, "commands-array-v1")
        references, actions, goal_statuses = {}, {}, {}
        for command, gid in zip(commands, task.goal_ids, strict=True):
            ref = "action_ref_" + fingerprint([evidence.evidence_id, command["order"], gid])[:24]
            references[gid] = ref
            actions[ref] = {**command, "goal_id": gid, "source_space_id": data["spaceId"],
                            "scene_version": data["version"], "evidence_id": evidence.evidence_id}
            goal_statuses[gid] = {"matched":"succeeded", "ambiguous":"clarification", "unmatched":"unavailable"}[command["status"]]
        return TaskResult(task_id=task.task_id,
            status="succeeded" if all(s == "succeeded" for s in goal_statuses.values()) else "partial",
            evidence_ids=[evidence.evidence_id], outputs={"action_ref": references, "actions": actions,
                "goal_statuses": goal_statuses, "scene_context_confirmed": ctx.scope.scene_version == data["version"]})

    async def dispatch(self, task: TaskSpec, action_ref: str, ctx: RunContext) -> TaskResult:
        if not ctx.settings.scene_actions_enabled:
            raise DomainError("actions_disabled", "动作输出尚未启用。", outcome="unsupported")
        action = next((result.outputs.get("actions", {}).get(action_ref)
                       for result in ctx.record.results.values()
                       if action_ref in result.outputs.get("actions", {})), None)
        if not action or action["goal_id"] not in task.goal_ids:
            raise DomainError("action_binding", "动作引用无效。")
        if action["status"] == "ambiguous":
            return TaskResult(task_id=task.task_id, status="clarification", code="ambiguous",
                              message="该动作有多个候选，请明确目标名称。")
        if action["status"] == "unmatched":
            return TaskResult(task_id=task.task_id, status="unavailable", code="unmatched",
                              message="该动作未匹配到当前空间指令。")
        if ctx.scene_invalidated:
            raise DomainError("scene_context_changed", "请待场景上下文更新后继续后续动作。", outcome="clarification")
        if action["source_space_id"] != ctx.scope.space_id or action["scene_version"] != ctx.scope.scene_version:
            raise DomainError("scene_version_mismatch", "请刷新当前场景版本后再执行动作。", outcome="clarification")
        evidence = await ctx.repository.evidence(ctx.scope.scope_hash, action["evidence_id"])
        if evidence is None:
            raise DomainError("action_expired", "动作来源已过期，请重新匹配。", outcome="clarification")
        exists, raw = resolve_pointer(evidence.payload, f"/data/commands/{action['order']-1}/result")
        if not exists or raw != action["result"] or not COMMAND.fullmatch(raw):
            raise DomainError("action_source", "动作编码与来源不一致。")
        action_id = "action_" + fingerprint([ctx.record.run_id, task.idempotency_key])[:24]
        if action_id in ctx.record.action_delivery:
            return TaskResult(task_id=task.task_id, status="partial", code="delivery_unknown",
                              message="该动作已有交付记录，未重复发送。")
        await ctx.emitter.emit("action_ready", {"action_id": action_id, "command_code": raw,
            "profile": "native", "delivery_status": "ready", "source_evidence_id": evidence.evidence_id,
            "source_space_id": action["source_space_id"], "scene_version": action["scene_version"],
            "command_order": list(ctx.goals).index(action["goal_id"]) + 1},
            task_id=task.task_id, goal_id=action["goal_id"])
        if action.get("intentType") in {"telepor", "scene_point", "game_point"}:
            ctx.scene_invalidated = True
        return TaskResult(task_id=task.task_id, evidence_ids=[evidence.evidence_id],
            message="动作已交给输出通道，执行结果等待客户端确认。", outputs={"action_id": action_id})
