"""
创建日期：2026-10-11
文件功能：验证知识引用成员、证据时效和完整性，不评估语义或临床正确性。
"""

import json
import re

from langchain_core.messages import ToolMessage

from meta_agent.contracts import DomainError, fingerprint
from meta_agent.domains.remote_knowledge import RemoteSnippet

REF = re.compile(r"[a-zA-Z0-9_-]+@[a-zA-Z0-9_.-]+#[a-zA-Z0-9_-]+")


async def audit_remote_answer(answer, history, ctx):
    remote = ctx.settings.knowledge_backend == "remote_rag"
    if not remote and not ctx.settings.business_grounding_enabled:
        return [], []
    evidence_tool = "knowledge.search.remote_rag" if remote else "knowledge.search"
    ids = {eid for r in ctx.record.results.values() for eid in r.evidence_ids}
    for message in history:
        if isinstance(message, ToolMessage) and isinstance(message.content, str):
            try:
                body = json.loads(message.content)
                if (
                    isinstance(body, dict)
                    and body.get("tool_name") == "search_knowledge"
                    and isinstance(body.get("evidence_id"), str)
                ):
                    ids.add(body["evidence_id"])
            except ValueError:
                continue
    allowed = set()
    for eid in ids:
        evidence = await ctx.repository.evidence(ctx.scope.scope_hash, eid)
        if not evidence or evidence.tool_name != evidence_tool:
            continue
        if fingerprint(evidence.payload) != evidence.raw_payload_hash:
            raise DomainError("knowledge_evidence_invalid", "保存的证据完整性校验失败。")
        if remote and (
            evidence.payload["retrieval"]["corpus_sha256"]
            != ctx.settings.knowledge_rag_corpus_sha256
        ):
            continue
        for snippet in evidence.payload["snippets"]:
            allowed.add(
                RemoteSnippet.model_validate(snippet).doc_ref if remote else snippet["doc_ref"]
            )
    cited = set(REF.findall(answer))
    current = [
        r
        for r in ctx.record.results.values()
        if (
            r.outputs.get("backend") == "remote_rag"
            if remote
            else r.outputs.get("tool_name") == "search_knowledge"
        )
    ]
    required = any(r.status == "succeeded" for r in current)
    invalid = cited - allowed
    valid = not invalid and (not required or bool(cited))
    ctx.record.metrics["knowledge_citation_audit"] = {
        "status": "passed" if valid else "failed",
        "cited_refs": sorted(cited),
        "invalid_refs": sorted(invalid),
        "available_ref_count": len(allowed),
        "semantic_support": "not_assessed",
    }
    if not valid:
        raise DomainError(
            "knowledge_citation_invalid", "回答缺少有效来源或引用了未获取的资料，已停止输出。"
        )
    fact_ids = [v.fact.fact_id for r in current for v in r.facts if v.fact.record_ref in cited]
    return sorted(cited), fact_ids
