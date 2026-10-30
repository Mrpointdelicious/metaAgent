"""
创建日期：2026-10-12
文件功能：企业知识库问答——直连 192.168.8.160/back/richtxt 拉取富文本，
再用 LLM 依据知识库内容回答（正文无匹配内容时回退通用知识）。

对应 Dify 链路：
  HTTP 请求 -> /back/richtxt（body {"richId": companykb}，headers apikey/apitoken）
  -> 代码节点 main({compkb})：JSON.parse(compkb).txt
  -> LLM 节点：依据正文回答，无匹配时用通用知识
本模块的 answer_from_kb 即该 LLM 节点；agent 只负责把基本问答路由到这里并转述结果。
"""

import json
from typing import Any

import httpx

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec
from meta_agent.llm.models import create_planner_model

RICH_TEXT_URL = "http://192.168.8.160/back/richtxt"
CONTRACT_VERSION = "company-kb-v1"
TIMEOUT = 15  # 秒（HTTP 拉取正文）

# 单次 LLM 回答可容纳的正文字符数（超出则先切块提取相关片段，再据此回答）
SINGLE_CALL_MAX = 20000
CHUNK_SIZE = 15000
MAX_CHUNKS = 8
EXCERPT_MAX = 4000  # 切块后合并给回答 LLM 的相关片段上限

_EXTRACT_SYSTEM = (
    "你是知识库检索助手。根据用户问题，从企业知识库正文中提取与问题直接相关的内容。"
    "只输出相关原文（可精简表述，不改写事实、不添加原文没有的信息）。"
    "若正文中没有与问题相关的内容，只输出：无相关内容"
)

_ANSWER_SYSTEM = (
    "你是企业服务助手。请依据企业知识库正文回答用户问题。规则如下："
    "1. 正文中有与问题相关的内容时，优先依据正文回答，不要编造、不要偏离；"
    "2. 正文中没有相关内容时，再依据你的通用知识回答；"
    "3. 用简洁的普通文本回答，不使用 Markdown 格式，不要输出 [Answer:] 之类的标记；"
    "4. 用用户提问所用的语言回答。"
)


def _to_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


async def fetch_rich_text(
    apikey: str, apitoken: str, rich_id: str, client: httpx.AsyncClient
) -> str:
    """POST /back/richtxt，按 Dify 代码节点逻辑抽取顶层 txt（不是 data.txt）。"""
    resp = await client.post(
        RICH_TEXT_URL,
        json={"richId": rich_id},
        headers={"apikey": apikey, "apitoken": apitoken, "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    doc = resp.json()
    if isinstance(doc, str):  # 兼容响应体被双重 JSON 编码
        try:
            doc = json.loads(doc)
        except Exception:
            doc = None
    if not isinstance(doc, dict):
        return ""
    # 对齐 Dify：直接用 comp.txt，不是 comp.data.txt
    return (doc.get("txt") or "").strip()


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _build_model(ctx: RunContext):
    """按 agent 模型配置建一个低温提取/回答模型（每轮工具调用新建，不共享）。"""
    return create_planner_model(
        ctx.settings.model_copy(
            update={
                "planner_model": ctx.settings.agent_model or ctx.settings.planner_model,
                "planner_temperature": 0.0,
                "planner_max_tokens": 4096,
                "planner_timeout_seconds": 25.0,
                "planner_max_retries": 0,
            }
        )
    )


async def _extract_one(model, query: str, chunk: str) -> str:
    messages = [
        ("system", _EXTRACT_SYSTEM),
        ("human", f"用户问题：{query}\n\n企业知识库正文：\n{chunk}"),
    ]
    result = await model.ainvoke(messages)
    return _model_content(result)


def _model_content(result) -> str:
    if isinstance(result, dict):
        content = result.get("content") or ""
    else:
        content = getattr(result, "content", "") or ""
    return _to_text(content)


async def _answer_one(model, query: str, context: str) -> str:
    ctx_text = context if context else "（无相关企业知识库内容）"
    messages = [
        ("system", _ANSWER_SYSTEM),
        ("human", f"用户问题：{query}\n\n企业知识库正文：\n{ctx_text}"),
    ]
    result = await model.ainvoke(messages)
    content = _model_content(result)
    if content == "无相关内容":
        content = ""
    return content


async def answer_from_kb(query: str, text: str, ctx: RunContext) -> str:
    """LLM 依据知识库内容回答；正文无匹配内容时回退通用知识。"""
    model = _build_model(ctx)
    if len(text) <= SINGLE_CALL_MAX:
        return await _answer_one(model, query, text)
    # 正文过大：先逐块提取相关片段，再依据片段回答
    excerpts: list[str] = []
    for chunk in _chunks(text, CHUNK_SIZE)[:MAX_CHUNKS]:
        try:
            excerpt = await _extract_one(model, query, chunk)
        except Exception:
            continue
        if excerpt and excerpt != "无相关内容":
            excerpts.append(excerpt)
    merged = "\n".join(excerpts).strip()[:EXCERPT_MAX]
    return await _answer_one(model, query, merged)


class CompanyKnowledgeAdapter:
    """拉取企业知识库富文本，用 LLM 依据内容作答，落证据库并投影为 TaskResult。"""

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        inputs = ctx.record.raw_inputs or {}
        apikey = _to_text(inputs.get("apikey"))
        apitoken = _to_text(inputs.get("apitoken"))
        rich_id = _to_text(inputs.get("companykb"))

        if not rich_id:
            raise DomainError(
                "company_kb_unavailable", "未提供企业知识库标识（companykb）。", outcome="unavailable"
            )
        if not apikey or not apitoken:
            raise DomainError(
                "company_kb_unavailable", "缺少知识库接口凭证（apikey/apitoken）。", outcome="unavailable"
            )

        try:
            txt = await fetch_rich_text(apikey, apitoken, rich_id, ctx.backend.client)
        except httpx.TimeoutException as exc:
            raise DomainError(
                "company_kb_timeout", "知识库检索超时，请稍后再试。", outcome="unavailable", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            message = (
                "未找到对应知识库内容。"
                if exc.response.status_code == 404
                else "知识库接口返回异常。"
            )
            raise DomainError("company_kb_unavailable", message, outcome="unavailable") from exc
        except httpx.RequestError as exc:
            raise DomainError(
                "company_kb_unavailable", "知识库接口暂不可用。", outcome="unavailable", retryable=True
            ) from exc

        # 无论正文是否为空，都由 LLM 依据内容作答（无匹配/空正文时回退通用知识）。
        answer = ""
        try:
            answer = await answer_from_kb(ctx.query, txt, ctx)
        except Exception:
            answer = ""
        message = answer or txt
        if not message:
            return TaskResult(
                task_id=task.task_id,
                status="unavailable",
                code="no_knowledge_evidence",
                message="知识库内容为空，暂时无法回答。",
                outputs={"tool_name": "search_company_knowledge", "rich_id": rich_id, "txt": ""},
            )

        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            "search_company_knowledge",
            ctx.record.request_id,
            {"rich_id": rich_id, "txt": txt, "answer": answer or None},
            CONTRACT_VERSION,
        )
        ctx.record.metrics["company_kb_txt_length"] = len(txt)
        ctx.record.metrics["company_kb_answered"] = bool(answer)
        return TaskResult(
            task_id=task.task_id,
            status="succeeded",
            evidence_ids=[evidence.evidence_id],
            message=message,
            outputs={
                "tool_name": "search_company_knowledge",
                "rich_id": rich_id,
                "answer": answer or None,
                "txt_length": len(txt),
                "contract_version": CONTRACT_VERSION,
            },
        )
