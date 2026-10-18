"""
创建日期：2026-10-18
文件功能：适配原生 MaxKB 分类检索，并校验空间、发布版本和来源。
"""

import asyncio
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException

from meta_agent.domains.maxkb_client import MaxKBClient, validate_url
from meta_agent.domains.maxkb_contract import RetrievalResponse, Snippet, canonical, estimate_tokens
from meta_agent.domains.maxkb_policy import (
    STRATEGIES,
    allowed_document,
    resolve_types,
    select_routes,
)

PINS = ("corpus_version", "corpus_sha256", "index_sha256", "scope_key", "space_ids")


def local_file(root, filename):
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ValueError("invalid_local_source")
    path = (root / filename).resolve()
    if path.parent != root.resolve():
        raise ValueError("invalid_local_source")
    return path


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def excerpt(text, query, limit=1600):
    if len(text) <= limit:
        return text, 0
    terms = re.findall(r"[A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,8}", query)
    positions = [text.lower().find(term.lower()) for term in terms]
    positions = [p for p in positions if p >= 0]
    start = min(max(0, min(positions) - 250) if positions else 0, len(text) - limit)
    return text[start : start + limit], start


class NativeMaxKBRetriever:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.path = Path(settings.maxkb_release_path)
        self.release, self.catalog = self.load_release()
        config = json.loads(Path(settings.maxkb_config_path).read_text(encoding="utf8"))
        if not config.get("username") or not config.get("password"):
            raise RuntimeError("MaxKB native admin-session credentials are required")
        self.client = MaxKBClient(http, settings.maxkb_url, config["username"], config["password"])
        self.public_base = validate_url(settings.maxkb_public_base_url, path="")
        self.limit = asyncio.Semaphore(2)

    def load_release(self):
        release = json.loads(self.path.read_text(encoding="utf8"))
        if (
            release.get("backend") != "maxkb"
            or release.get("schema_version") != 2
            or not release.get("scope_key")
            or not release.get("space_ids")
            or any(not isinstance(s, str) or not s.strip() for s in release["space_ids"])
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", release.get(k, ""))
                for k in ("corpus_sha256", "index_sha256")
            )
        ):
            raise RuntimeError("Invalid pinned MaxKB release")
        catalog = json.loads(
            local_file(self.path.parent, release["catalog_file"]).read_text("utf8")
        )
        if (
            catalog.get("schema_version") != 2
            or catalog.get("strategies") != STRATEGIES
            or hashlib.sha256(canonical(catalog)).hexdigest() != release["index_sha256"]
            or hashlib.sha256(canonical(catalog["documents"])).hexdigest()
            != release["corpus_sha256"]
        ):
            raise RuntimeError("MaxKB release integrity/policy check failed")
        return release, catalog

    def assert_current(self):
        current, _ = self.load_release()
        if any(current[k] != self.release[k] for k in PINS):
            raise HTTPException(409, "maxkb_release_changed_restart_required")

    def authorize(self, state):
        space = state.get("space_id")
        if not space:
            raise HTTPException(422, "maxkb_space_id_required")
        if (
            space not in self.release["space_ids"]
            or state.get("tenant_id") != self.release["scope_key"]
            or state.get("role") != "operator"
            or state.get("patient_id")
        ):
            raise HTTPException(403, "maxkb_scope_denied")

    async def retrieve(self, state, domain, *, types=None, top_k=None):
        self.authorize(state)  # Must precede login and native recall.
        started = time.monotonic()
        async with asyncio.timeout(self.settings.maxkb_timeout_seconds), self.limit:
            await asyncio.to_thread(self.assert_current)
            routes = select_routes(
                self.catalog, state["space_id"], resolve_types(domain, state["query"], types)
            )
            semaphore = asyncio.Semaphore(3)

            async def recall(route):
                profile = STRATEGIES[route["knowledge_type"]]
                async with semaphore:
                    rows = await self.client.call(
                        "POST",
                        f"/workspace/default/knowledge/{route['knowledge_id']}/hit_test",
                        json={
                            "query_text": state["query"],
                            "top_number": profile["candidates"],
                            "similarity": 0,
                            "search_mode": profile["mode"],
                        },
                    )
                if not isinstance(rows, list) or len(rows) > 500:
                    raise ValueError("invalid_hit_test_response")
                hits = []
                for row in rows:
                    meta = self.catalog["paragraphs"].get(row["id"])
                    if not meta:
                        continue  # UI uploads require an explicit publication first.
                    doc = self.catalog["documents"].get(meta["document_id"], {})
                    if (
                        meta["knowledge_id"] != route["knowledge_id"]
                        or meta["document_id"] != row["document_id"]
                    ):
                        raise HTTPException(409, "maxkb_candidate_scope_mismatch")
                    if (
                        not meta.get("published")
                        or not allowed_document(doc, route, state["space_id"])
                        or not row.get("is_active", True)
                    ):
                        continue
                    if row["content"] != meta["text"]:
                        raise HTTPException(409, "maxkb_knowledge_changed_publish_required")
                    score = row.get("comprehensive_score")
                    score = row.get("similarity", 0) if score is None else score
                    if type(score) not in (int, float) or not math.isfinite(score):
                        raise ValueError("invalid_native_score")
                    hits.append({**row, "score": float(score), "meta": meta, "route": route})
                # Native hit_test may hydrate results in database order.
                hits.sort(key=lambda h: (-h["score"], h["id"]))
                return [{**h, "rank": rank} for rank, h in enumerate(hits, 1)]

            tasks = [asyncio.create_task(recall(r)) for r in routes]
            try:
                groups = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            hits = sorted(
                [h for group in groups for h in group], key=lambda h: (h["rank"], h["id"])
            )
            unique = {}
            for hit in hits:
                unique.setdefault((hit["route"]["knowledge_type"], hit["content"]), hit)
            hits = list(unique.values())[:80]
            # Preserve per-KB ranking without pretending scores across types are calibrated.
            ranked = hits
            wants_rerank = any(STRATEGIES[r["knowledge_type"]]["rerank"] for r in routes)
            evidence, tokens, truncated = [], 0, False
            live_docs, live_rows = {}, {}
            for hit in ranked:
                meta, route = hit["meta"], hit["route"]
                doc = self.catalog["documents"][meta["document_id"]]
                if doc["id"] not in live_docs:
                    try:
                        live_docs[doc["id"]] = await self.client.call(
                            "GET", self.document_path(doc)
                        )
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        live_docs[doc["id"]] = {}
                if not live_docs[doc["id"]].get("is_active"):
                    continue
                if doc["id"] not in live_rows:
                    rows = await self.client.call("GET", self.document_path(doc) + "/paragraph")
                    live_rows[doc["id"]] = {p["id"]: p for p in rows if p.get("is_active")}
                live = live_rows[doc["id"]].get(hit["id"])
                if not live:
                    continue
                if live["content"] != meta["text"] or live.get("title", "") != meta["title"]:
                    raise HTTPException(409, "maxkb_knowledge_changed_publish_required")
                text, offset = excerpt(meta["text"], state["query"])
                truncated |= offset > 0 or len(text) < len(meta["text"])
                source = (
                    self.public_base
                    + "/v1/knowledge/sources/"
                    + doc["code"]
                    + "?"
                    + urlencode(
                        {"space_id": state["space_id"], "tenant_id": self.release["scope_key"]}
                    )
                )
                section, version = (
                    "c-" + hit["id"].replace("-", ""),
                    "v-" + doc["revision_sha256"][:12],
                )
                start = meta.get("start", 0) + offset
                snippet = Snippet(
                    source_id=uuid5(NAMESPACE_URL, source),
                    chunk_id=uuid5(NAMESPACE_URL, f"{source}@{version}#{section}"),
                    doc_ref=f"{doc['code']}@{version}#{section}",
                    document_version=version,
                    title=doc["name"][:200],
                    source=source,
                    section=(meta["title"] or "正文")[:200],
                    section_id=section,
                    page=meta.get("page"),
                    page_end=meta.get("page_end"),
                    start=start,
                    end=start + len(text),
                    text=text,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    score=hit["score"],
                    space_id=state["space_id"],
                    scope_kind="space",
                    knowledge_type=route["knowledge_type"],
                    source_group=doc["group"],
                    heading_path=meta.get("heading_path", ""),
                    offset_basis=meta.get("offset_basis", "chunk_text"),
                )
                cost = estimate_tokens(snippet.model_dump(mode="json"))
                if tokens + cost > self.settings.maxkb_context_budget:
                    truncated = True
                    continue
                evidence.append(snippet)
                tokens += cost
                if len(evidence) >= (top_k or self.settings.maxkb_top_k):
                    break
            await asyncio.to_thread(self.assert_current)
        return RetrievalResponse(
            request_id=uuid4(),
            retrieval_run_id=uuid4(),
            backend="maxkb",
            strategy="hybrid_selection",
            scope_key=self.release["scope_key"],
            domain=domain,
            **{k: self.release[k] for k in ("corpus_version", "corpus_sha256", "index_sha256")},
            status=("partial" if wants_rerank else "success") if evidence else "unavailable",
            code=("native_rerank_not_available" if wants_rerank else "ok")
            if evidence
            else "no_evidence",
            elapsed_ms=(time.monotonic() - started) * 1000,
            reranker_used=None,
            rerank_applied=False,
            degradation=["native_hit_test_has_no_rerank"] if wants_rerank else [],
            candidate_count=len(hits),
            post_rerank_count=len(ranked),
            selected_count=len(evidence),
            budget_truncated=truncated,
            context_tokens_estimated=tokens,
            model_calls={"rerank": 0},
            stages={
                "implementation": "maxkb-v2.10.7-lts-native",
                "space_id": state["space_id"],
                "knowledge_types": list(resolve_types(domain, state["query"], types)),
                "routes": [
                    {
                        "knowledge_id": r["knowledge_id"],
                        "mode": STRATEGIES[r["knowledge_type"]]["mode"],
                        "rerank_applied": False,
                    }
                    for r in routes
                ],
                "maxkb_api": "admin_session",
                "native_recall_requests": len(routes),
                "model_call_accounting": "embedding_managed_by_maxkb_not_observed",
                "authorization": "before_recall_and_on_source_access",
            },
            evidence=evidence,
        )

    @staticmethod
    def document_path(doc):
        return f"/workspace/default/knowledge/{doc['knowledge_id']}/document/{doc['id']}"

    async def source_file(self, code, state):
        self.authorize(state)
        async with asyncio.timeout(self.settings.maxkb_timeout_seconds):
            await asyncio.to_thread(self.assert_current)
            doc = next((d for d in self.catalog["documents"].values() if d["code"] == code), None)
            routes = select_routes(self.catalog, state["space_id"], tuple(STRATEGIES))
            if not doc or not any(allowed_document(doc, r, state["space_id"]) for r in routes):
                raise HTTPException(404, "maxkb_source_not_found")
            live = await self.client.call("GET", self.document_path(doc))
            if not live.get("is_active"):
                raise HTTPException(404, "maxkb_source_disabled")
            path = local_file(
                self.path.parent / "originals", doc.get("citation_file", doc["original_file"])
            )
            if not path.is_file() or await asyncio.to_thread(file_hash, path) != doc.get(
                "citation_sha256", doc["sha256"]
            ):
                raise HTTPException(409, "maxkb_source_integrity_failed")
            await asyncio.to_thread(self.assert_current)
            name = doc["name"]
            if doc.get("citation_kind") == "knowledge_snapshot":
                name += ".knowledge.json"
            return path, name
