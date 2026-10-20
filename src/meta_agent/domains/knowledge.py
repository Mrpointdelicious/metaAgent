"""
创建日期：2026-09-12
文件功能：检索显式批准的分节知识语料，返回原文与章节引用。
"""

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, StrictModel, TaskResult, TaskSpec
from meta_agent.domains.facts import FactBuilder


class Section(StrictModel):
    section_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=4000)


class Document(StrictModel):
    doc_id: str = Field(min_length=1, max_length=100)
    domain: Literal["health", "product", "help"]
    version: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=1000)
    approved: bool = False
    approved_by: str = ""
    sections: list[Section] = Field(min_length=1, max_length=200)


class Corpus(StrictModel):
    schema_version: Literal["1.0"]
    documents: list[Document] = Field(max_length=1000)


def terms(text: str) -> list[str]:
    # Chinese bigrams preserve word fragments without a runtime dictionary download.
    output = re.findall(r"[a-z0-9_]+", text.lower())
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        output.extend(word[i : i + 2] for i in range(len(word) - 1))
    return output


def rank(query: str, sections: list[tuple[Document, Section]]) -> list[tuple[float, int]]:
    counters = [
        Counter(terms(doc.title + " " + section.title + " " + section.text))
        for doc, section in sections
    ]
    avg = sum(sum(c.values()) for c in counters) / max(1, len(counters))
    scores = []
    for i, counter in enumerate(counters):
        score = 0.0
        for term in set(terms(query)):
            tf = counter[term]
            df = sum(term in c for c in counters)
            if tf:
                idf = math.log(1 + (len(counters) - df + 0.5) / (df + 0.5))
                score += (
                    idf
                    * tf
                    * 2.2
                    / (tf + 1.2 * (0.25 + 0.75 * sum(counter.values()) / max(1, avg)))
                )
        if score > 0:
            scores.append((score, i))
    return sorted(scores, reverse=True)


class KnowledgeAdapter:
    async def execute(self, task: TaskSpec, args: dict, ctx: RunContext) -> TaskResult:
        path = Path(ctx.settings.knowledge_corpus_path)
        try:
            if not path.is_file() or path.stat().st_size > 10_000_000:
                raise ValueError("missing or oversized corpus")
            corpus = Corpus.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            raise DomainError(
                "knowledge_unavailable", "已批准知识库暂不可用。", outcome="unavailable"
            ) from exc
        sections = [
            (doc, section)
            for doc in corpus.documents
            if doc.approved and doc.approved_by.strip() and doc.domain == args["domain"]
            for section in doc.sections
        ]
        found = rank(args["query"], sections)
        rewritten = False
        if not found:
            rewritten = True
            query = args["query"]
            for source, target in {"走路": "步行", "练习": "训练", "怎么用": "使用方法"}.items():
                query = query.replace(source, target)
            found = rank(query, sections)
        if not found:
            return TaskResult(
                task_id=task.task_id,
                status="unavailable",
                code="no_knowledge_evidence",
                message="未检索到已批准的相关资料，暂不能据此作答。",
                outputs={"query_rewritten": rewritten},
            )
        snippets = []
        for _, index in found[:3]:
            doc, section = sections[index]
            snippets.append(
                {
                    "doc_ref": f"{doc.doc_id}@{doc.version}#{section.section_id}",
                    "title": doc.title,
                    "section": section.title,
                    "source": doc.source,
                    "text": section.text,
                }
            )
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            "knowledge.search",
            ctx.record.request_id,
            {"snippets": snippets},
            "approved-corpus-v1",
        )
        builder = FactBuilder(evidence)
        for index, snippet in enumerate(snippets):
            view = builder.add(
                f"/snippets/{index}/text",
                "knowledge_excerpt",
                f"{snippet['title']} · {snippet['section']}",
                required=True,
                uses=["display", "knowledge_explanation"],
            )
        for view in builder.views:
            view.fact.source_type = "document"
        return TaskResult(
            task_id=task.task_id,
            evidence_ids=[evidence.evidence_id],
            facts=builder.views,
            outputs={"doc_refs": [s["doc_ref"] for s in snippets], "query_rewritten": rewritten},
        )
