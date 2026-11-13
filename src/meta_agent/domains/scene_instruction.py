"""
创建日期：2026-10-30
文件功能：适配分支业务能力并保留可信范围边界。
Natural-language intent → local scene resolver → validated native delivery.

场景目录加载、拆句、同义词/拼音排序、歧义判断均由 agent 侧的
meta_agent.domains.scene_local 本地完成（直连点位库，不经过 AI_WebApi）。
Python 侧仍校验有序原话覆盖，绝不自行推断可执行指令编码。
"""

import re

from meta_agent.contracts import DomainError, Goal, TaskResult
from meta_agent.domains.business_routing import classify_business_query
from meta_agent.domains.scene import SceneAdapter, numeric_envelope, source_text

# 「退回上一个场景」这类浏览历史话术的识别器。旧版 AI_WebApi 目录没有对应点位，
# 曾用于给出“暂不支持”的确定性引导；真实目录（version=138）里有
# [ClickPoint:]33「退回上个场景」，本地解析器已能匹配，因此不再拦截执行。
# 保留函数供日志/兼容判断使用。
_HISTORY_BACK = re.compile(
    r"(?:返回|退回|后退|回到|回|退)(?:到)?(?:上一个|上一|上个|前一个|刚才那个)"
    r"(?:场景|页面|界面|屏幕|地方)?|上一步"
)


def is_history_back(text: str) -> bool:
    """判断是否“退回上一个场景/上一步”这类历史回退话术。"""
    return bool(_HISTORY_BACK.search(source_text(text)))


class SceneIntentParser:
    # 口语前缀（敬语/意愿/带路），AI_WebApi 指令匹配器不识别，归一时剥掉。
    _WISH_PREFIX = re.compile(r"^(?:请|麻烦|帮我|我想|我想要|我要|带我)+")

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

    def canonicalize(self, target: str) -> str:
        """把「我想回四楼」这类口语归一成「去四楼」。

        这套改写源自 AI_WebApi 匹配器只认「去X / 带我去X / 回到X / 返回X」的
        历史限制。本地解析器（scene_local）本身已覆盖全部口语前缀，改写在这里
        属于无害的兼容层：只剥前缀、换一个字，剩余仍是 parse() 已验证的原话
        片段，不破坏来源校验；也保证回滚到 .NET 匹配器时行为不变。
        无需改写时原样返回。
        """
        raw = source_text(target)
        if is_history_back(raw):
            # 「回上一个场景」不是「去X」，改写会改变语义，原样交给上层判定。
            return target
        core = self._WISH_PREFIX.sub("", raw) or raw
        if core.startswith("回") and not core.startswith(("回到", "返回")):
            rewritten = "去" + core[1:]
        else:
            rewritten = core
        if rewritten == raw:
            return target
        return rewritten

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
        # 与 scene_local._SPLIT_RE 的切分口径一致：拆句时丢掉的连接词必须在这里放行，
        # 否则 spans() 会误判成「指令遗漏了原话内容」。标点由 source_text() 剥离，
        # 但顿号「、」不在其中，需显式列入。
        return (
            re.fullmatch(
                r"(?:(?:然后|接着|随后|并且|并|再|和|请|帮我|麻烦|一下"
                r"|同时|顺便|以及|还有|、))*",
                text,
            )
            is not None
        )


class SceneCommandResolver:
    async def resolve(self, task, args, ctx):
        if not ctx.settings.scene_enabled or not ctx.scope.space_id:
            raise DomainError(
                "scene_unavailable", "当前空间场景服务不可用。", outcome="unavailable"
            )
        parser = SceneIntentParser()
        target = parser.parse(args["target"], ctx)
        # 本地解析器自行识别全部口语前缀，原话透传即可；不再做 canonicalize 改写，
        # 否则 spans() 会因后端返回的 sourceText 与原话不一致而误判 command_alignment。
        # canonicalize 保留为回滚到 .NET 匹配器时的兼容层。
        payload = {"target": target, "spaceId": ctx.scope.space_id}
        if ctx.scope.scene_version is not None:
            payload["expectedSceneVersion"] = ctx.scope.scene_version
        body = await ctx.call("navigate_scene", payload)
        data = numeric_envelope(body)
        commands = data.get("commands")
        if not isinstance(commands, list):
            raise DomainError("invalid_contract", "场景服务未返回指令数组。")
        if data.get("spaceId") != ctx.scope.space_id:
            raise DomainError("scene_scope_mismatch", "场景目录与当前空间不一致。")
        if type(data.get("version")) is not int or data["version"] < 0:
            raise DomainError("invalid_contract", "场景服务未返回有效版本。")
        if ctx.scope.scene_version is not None and data["version"] != ctx.scope.scene_version:
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


def is_command_query(query):
    return classify_business_query(query).scene


def is_navigation_question(query):
    return classify_business_query(query).navigation_question


def is_knowledge_question(query):
    return classify_business_query(query).knowledge


def is_mixed_command_query(query):
    route = classify_business_query(query)
    return route.scene and (route.knowledge or route.patient or route.history)
