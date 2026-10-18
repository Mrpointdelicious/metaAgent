"""
创建日期：2026-10-11
文件功能：定义独立retrieval-v1协议，验证来源定位与预算计数。
"""

import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

Strategy = Literal["bm25", "dense", "hybrid", "hybrid_rerank", "hybrid_selection"]


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def estimate_tokens(value: object) -> int:
    # Conservative UTF-8 byte upper bound, NOT the embedding/Agent billing tokenizer.
    return len(canonical(value)) if isinstance(value, dict) else len(str(value).encode())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RetrievalQuery(Model):
    schema_version: Literal["retrieval-v1"] = "retrieval-v1"
    request_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    domain: Literal["health", "product", "help"] = "health"
    scope_key: str = Field(min_length=1, max_length=100)
    space_id: str | None = Field(default=None, min_length=1, max_length=160)
    corpus_version: str = Field(min_length=1, max_length=100)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: Strategy = "hybrid_selection"
    top_k: int = Field(default=6, ge=1, le=6, strict=True)
    context_budget: int = Field(default=6000, ge=100, le=12000, strict=True)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.query.strip():
            raise ValueError("Blank query")
        return self


class Snippet(Model):
    source_id: UUID
    chunk_id: UUID
    doc_ref: str = Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+#[a-z0-9-]+$", max_length=200)
    document_version: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(pattern=r"^(?:repo|https|http)://[^\s]+$", max_length=500)
    section: str = Field(min_length=1, max_length=200)
    section_id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    page: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    space_id: str | None = None
    scope_kind: Literal["space", "shared"] = "space"
    knowledge_type: str = ""
    source_group: str = ""
    heading_path: str = ""
    offset_basis: str = "chunk_text"
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=1600)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float

    @model_validator(mode="after")
    def consistency(self):
        code = self.doc_ref.split("@", 1)[0]
        if (
            self.doc_ref != f"{code}@{self.document_version}#{self.section_id}"
            or (self.source.startswith("repo://") and self.source.rsplit("/", 1)[-1] != code)
            or self.source_id != uuid5(NAMESPACE_URL, self.source)
            or self.chunk_id
            != uuid5(NAMESPACE_URL, f"{self.source}@{self.document_version}#{self.section_id}")
            or self.end - self.start != len(self.text)
            or self.text_sha256 != digest(self.text.encode())
        ):
            raise ValueError("Inconsistent evidence location/hash")
        return self


class RetrievalResponse(Model):
    schema_version: Literal["retrieval-v1"] = "retrieval-v1"
    request_id: UUID
    retrieval_run_id: UUID
    backend: Literal["portfolio_hybrid", "maxkb"] = "portfolio_hybrid"
    strategy: Strategy
    approval: Literal["technical_demo_only"] = "technical_demo_only"
    clinical_approval: Literal[False] = False
    scope_key: str
    domain: Literal["health", "product", "help"]
    corpus_version: str
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["success", "partial", "unavailable", "failed"]
    code: str
    elapsed_ms: float = Field(ge=0)
    reranker_used: str | None
    rerank_applied: bool
    degradation: list[str]
    candidate_count: int = Field(ge=0, le=80)
    post_rerank_count: int = Field(ge=0, le=80)
    selected_count: int = Field(ge=0, le=6)
    budget_truncated: bool
    context_tokens_estimated: int = Field(ge=0)
    token_estimator: Literal["utf8-bytes-upper-bound-v1"] = "utf8-bytes-upper-bound-v1"
    model_calls: dict[str, int]
    stages: dict
    evidence: list[Snippet] = Field(max_length=6)
    signature: str = ""

    @model_validator(mode="after")
    def counts(self):
        if (
            self.selected_count != len(self.evidence)
            or not self.selected_count <= self.post_rerank_count <= self.candidate_count
            or (self.status in {"success", "partial"}) != bool(self.evidence)
            or len({s.doc_ref for s in self.evidence}) != len(self.evidence)
            or (self.rerank_applied and not self.reranker_used)
            or self.context_tokens_estimated
            != sum(estimate_tokens(s.model_dump(mode="json")) for s in self.evidence)
        ):
            raise ValueError("Inconsistent retrieval envelope")
        return self
