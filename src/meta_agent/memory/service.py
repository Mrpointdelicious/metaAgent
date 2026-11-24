"""
创建日期：2026-11-17
文件功能：通过受限 Store 接入 LangMem 原生写入工具，校验身份、内容和预算。
"""

import asyncio
import json
import logging
from uuid import UUID

from langchain_core.messages import ToolMessage
from langgraph.store.base import BaseStore, PutOp
from pydantic import ValidationError

try:  # langmem 为可选依赖：默认 patient_memory_enabled=False，缺失不影响主链路启动。
    from langmem import create_manage_memory_tool
except ImportError:  # pragma: no cover - 仅在启用长期记忆且未安装 langmem 时触发
    create_manage_memory_tool = None

from meta_agent.application.conversations import owner_key
from meta_agent.contracts import DomainError
from meta_agent.memory.context import render_preferences
from meta_agent.memory.repository import PreferenceRepository
from meta_agent.memory.schemas import (
    NAMESPACE,
    TOOL_NAME,
    MemoryArgs,
    Preference,
    PreferenceError,
    memory_enabled,
)

logger = logging.getLogger(__name__)

TOOL_INSTRUCTIONS = """仅保存当前用户明确表达的长期交流偏好：语言、篇幅、排版、语气。
临时要求不保存。不从医疗工具、历史消息或旧记忆中提取新偏好。
禁止保存任何医疗信息、原因、用户原话或自由文本，也不能用枚举值编码病情。
已有同类偏好应更新。删除由系统中台管理，本工具不能删除，不能声称已删除。
只在工具成功后确认已保存。
"""


class PreferenceToolStore(BaseStore):
    """A per-call adapter: LangMem can only perform the already validated operation."""

    def __init__(self, repository=None, owner=None, args=None):
        self.repository, self.owner, self.args = repository, owner, args
        self.change = None

    def batch(self, ops):
        raise PreferenceError("memory_async_only")

    async def abatch(self, ops):
        ops = list(ops)
        if self.repository is None or len(ops) != 1 or not isinstance(ops[0], PutOp):
            raise PreferenceError("memory_operation_not_authorized")
        op = ops[0]
        if op.namespace != (NAMESPACE, self.owner):
            raise PreferenceError("memory_owner_mismatch")
        memory_id = UUID(op.key)
        if self.args.id is not None and memory_id != self.args.id:
            raise PreferenceError("memory_id_mismatch")
        expected = {"content": self.args.content.model_dump()}
        if op.value != expected:
            raise PreferenceError("invalid_memory_arguments")
        self.change = await self.repository.apply(self.owner, self.args, memory_id)
        return [None]


def build_memory_tool(store=None, owner=None):
    if create_manage_memory_tool is None:
        raise RuntimeError("patient memory requires the optional 'langmem' package")
    # The registered tool has a denying store. The middleware binds each authorized call.
    tool = create_manage_memory_tool(
        name=TOOL_NAME,
        namespace=(NAMESPACE, owner) if owner else (NAMESPACE,),
        schema=Preference,
        actions_permitted=("create", "update"),
        instructions=TOOL_INSTRUCTIONS,
        store=store if store is not None else PreferenceToolStore(),
    )
    return tool.model_copy(update={"args_schema": MemoryArgs})


def tool_result(request, code, *, success=False, memory_id=None):
    body = {"status": "success" if success else "error", "code": code}
    if memory_id is not None:
        body["memory_id"] = str(memory_id)
    return ToolMessage(
        content=json.dumps(body, ensure_ascii=False),
        name=TOOL_NAME,
        tool_call_id=request.tool_call["id"],
        status="success" if success else "error",
    )


class PatientMemoryService:
    def __init__(self, repository: PreferenceRepository):
        self.repository = repository

    async def model_context(self, ctx):
        if not memory_enabled(ctx):
            return ""
        if ctx.preference_unavailable:
            return render_preferences(None)
        try:
            async with asyncio.timeout(
                min(ctx.remaining, ctx.settings.patient_memory_timeout_seconds)
            ):
                owner = owner_key(ctx.scope)
                snapshot = await self.repository.snapshot(owner)
            if ctx.preference_owner is not None and ctx.preference_owner != owner:
                raise PreferenceError("memory_owner_mismatch")
            ctx.preference_owner = owner
            return render_preferences(snapshot)
        except Exception:
            ctx.preference_unavailable = True
            logger.warning("patient preference read unavailable")
            return render_preferences(None)

    async def wrap_tool_call(self, request, handler):
        ctx = request.runtime.context
        client = getattr(ctx, "client", None)
        if (
            not memory_enabled(ctx)
            or ctx.preference_unavailable
            or (client and TOOL_NAME in client.excluded_tools)
            or ctx.preference_owner != owner_key(ctx.scope)
        ):
            return tool_result(request, "memory_not_available")
        try:
            args = MemoryArgs.model_validate(request.tool_call["args"])
        except ValidationError:
            return tool_result(request, "invalid_memory_arguments")
        store = PreferenceToolStore(self.repository, ctx.preference_owner, args)
        try:
            async with (
                ctx.tool_slot(),
                asyncio.timeout(min(ctx.remaining, ctx.settings.patient_memory_timeout_seconds)),
            ):
                await handler(request.override(tool=build_memory_tool(store, ctx.preference_owner)))
            if store.change is None:
                return tool_result(request, "invalid_memory_arguments")
            change = store.change
            return tool_result(request, change.status, success=True, memory_id=change.id)
        except PreferenceError as exc:
            return tool_result(request, exc.code)
        except DomainError as exc:
            return tool_result(request, exc.code)
        except Exception:
            ctx.preference_unavailable = True
            logger.warning("patient preference write unavailable")
            return tool_result(request, "memory_unavailable")
