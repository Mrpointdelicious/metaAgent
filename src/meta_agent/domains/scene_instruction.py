"""
创建日期：2026-10-30
文件功能：适配分支业务能力并保留可信范围边界。
Natural-language intent → AI_WebApi resolver → validated native delivery.

AI_WebApi owns catalog loading, splitting, synonym/pinyin ranking and ambiguity.
Python verifies ordered source coverage and never infers executable command IDs.
"""

import re

from meta_agent.contracts import DomainError, Goal, TaskResult
from meta_agent.domains.scene import SceneAdapter, numeric_envelope, source_text


class SceneIntentParser:
    def parse(self, target, ctx):
        if not target.strip() or source_text(target) not in source_text(ctx.query):
            raise DomainError(
                "scene_request_mismatch", "动作须来自本轮原话。", outcome="clarification"
            )
        if re.search(r"不要|别|禁止|不许|如果|能否|可不可以|是否", ctx.query):
            raise DomainError(
                "scene_intent_uncertain", "请明确希望执行的动作。", outcome="clarification"
            )
        return target

    def spans(self, target, commands):
        normalized, offset, spans = source_text(target), 0, []
        for command in commands:
            if not isinstance(command, dict):
                raise DomainError("invalid_contract", "场景目录返回格式无效。")
            text = source_text(str(command.get("sourceText") or ""))
            position = normalized.find(text, offset) if text else -1
            if position < 0 or not self.connector(normalized[offset:position]):
                raise DomainError(
                    "command_alignment", "指令未按原话顺序完整对齐。", outcome="clarification"
                )
            spans.append(command["sourceText"])
            offset = position + len(text)
        if not self.connector(normalized[offset:]):
            raise DomainError("command_alignment", "指令遗漏了原话内容。", outcome="clarification")
        return spans

    @staticmethod
    def connector(text):
        return (
            re.fullmatch(r"(?:(?:然后|接着|随后|并且|并|再|和|请|帮我|麻烦|一下))*", text)
            is not None
        )


class SceneCommandResolver:
    async def resolve(self, task, args, ctx):
        if not ctx.settings.scene_enabled or not ctx.scope.space_id:
            raise DomainError(
                "scene_unavailable", "当前空间场景服务不可用。", outcome="unavailable"
            )
        if ctx.scope.scene_version is None:
            raise DomainError(
                "scene_version_required", "请先确认当前场景版本。", outcome="clarification"
            )
        parser = SceneIntentParser()
        target = parser.parse(args["target"], ctx)
        body = await ctx.call(
            "navigate_scene",
            {
                "target": target,
                "spaceId": ctx.scope.space_id,
                "expectedSceneVersion": ctx.scope.scene_version,
            },
        )
        data = numeric_envelope(body)
        commands = data.get("commands")
        if not isinstance(commands, list):
            raise DomainError("invalid_contract", "场景服务未返回指令数组。")
        if data.get("spaceId") != ctx.scope.space_id:
            raise DomainError("scene_scope_mismatch", "场景目录与当前空间不一致。")
        if type(data.get("version")) is not int or data["version"] != ctx.scope.scene_version:
            raise DomainError(
                "scene_version_mismatch", "场景版本已变化，请刷新后重试。", outcome="clarification"
            )
        if not commands:
            return TaskResult(
                task_id=task.task_id,
                status="unavailable",
                code="scene_catalog_unavailable",
                message="当前没有可用场景点位或可解析指令。",
            )
        spans = parser.spans(target, commands)
        ids = [f"{task.task_id}_scene_{i}" for i in range(len(spans))]
        for gid, span in zip(ids, spans, strict=True):
            ctx.goals[gid] = Goal(goal_id=gid, kind="scene_action", domain="scene", query_span=span)
        task = task.model_copy(update={"goal_ids": ids})
        return await SceneAdapter().resolve_response(task, args, ctx, body)


class SceneCommandExecutor:
    async def execute(self, task, result, ctx):
        dispatches = []
        adapter = SceneAdapter()
        for gid, ref in result.outputs.get("action_ref", {}).items():
            child = task.model_copy(update={"goal_ids": [gid], "capability": "scene.dispatch"})
            try:
                outcome = await adapter.dispatch(child, ref, ctx)
            except DomainError as exc:
                outcome = TaskResult(
                    task_id=task.task_id, status=exc.outcome, code=exc.code, message=exc.message
                )
            dispatches.append(
                {
                    "status": outcome.status,
                    "code": outcome.code,
                    "message": outcome.message,
                    **outcome.outputs,
                }
            )
        result.outputs["dispatches"] = dispatches
        if any(row["status"] != "succeeded" for row in dispatches):
            result.status = "partial"
        result.message = "指令已解析；动作交付状态须单独确认。"
        return result


class SceneInstructionAdapter:
    async def execute(self, task, args, ctx, *, dispatch=True):
        result = await SceneCommandResolver().resolve(task, args, ctx)
        ctx.record.results[task.task_id] = result
        if dispatch:
            return await SceneCommandExecutor().execute(task, result, ctx)
        result.message = result.message or "仅完成指令匹配，尚未交付动作。"
        return result
