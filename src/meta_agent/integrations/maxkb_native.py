"""
创建日期：2026-10-27
文件功能：直接调用MaxKB原生API，保留已发布资料、医院空间和原文引用边界。
"""

import asyncio
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import httpx

from meta_agent.context.budget import estimate_tokens
from meta_agent.integrations.knowledge import (
    KnowledgeAccessDenied,
    KnowledgeContractError,
    validate_contract,
)

DOMAIN_TYPES = {
    "health": {"clinical_kb", "rehab_kb"},
    "help": {"sop_kb"},
    "product": {"scene_kb", "hospital_kb", "device_kb", "sop_kb", "technical_kb"},
}


@lru_cache(maxsize=4)
def catalog(path, expected_hash):
    file = Path(path)
    try:
        if file.stat().st_size > 64_000_000:
            raise KnowledgeContractError("maxkb_catalog_too_large")
        value = json.loads(file.read_text(encoding="utf-8"))
    except OSError:
        raise KnowledgeContractError("maxkb_catalog_unavailable") from None
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    if value.get("schema_version") != 2 or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise KnowledgeContractError("maxkb_catalog_integrity_failed")
    return value


def identifier(value):
    return str(UUID(str(value)))


class NativeClient:
    def __init__(self, http, source, max_bytes):
        self.http, self.source, self.max_bytes = http, source, max_bytes
        self.token = ""

    async def call(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        async with self.http.stream(
            method, str(self.source.url).rstrip("/") + path, headers=headers, json=body
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for part in response.aiter_bytes():
                raw.extend(part)
                if len(raw) > self.max_bytes:
                    raise KnowledgeContractError("oversized_maxkb_response")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("code") not in (0, 200):
            # MaxKB may return HTTP 200 for provider failures; never expose its error body.
            if isinstance(value, dict) and value.get("code") in (401, 403):
                raise KnowledgeAccessDenied("maxkb_access_denied")
            raise KnowledgeContractError("maxkb_native_request_failed")
        return value.get("data")

    async def login(self):
        username, password = self.source.maxkb.credentials()
        if not username or not password:
            raise KnowledgeContractError("maxkb_credential_missing")
        value = await self.call("POST", "/user/login", {"username": username, "password": password})
        if not isinstance(value, dict) or not value.get("token"):
            raise KnowledgeContractError("maxkb_login_failed")
        self.token = value["token"]


def project(rows, budget, trace_id):
    context, sources, seen = "", [], set()
    truncated = False
    for doc, meta, text in rows:
        section_id = "c-" + identifier(meta["id"]).replace("-", "")
        ref = f"{doc['code']}@v-{doc['revision_sha256'][:12]}#{section_id}"
        if ref in seen:
            continue
        seen.add(ref)
        locator = meta.get("title") or "正文"
        if meta.get("page"):
            locator = f"PDF 物理页 {meta['page']} · {locator}"
        source = {
            "id": f"S{len(sources) + 1}",
            "title": doc["name"],
            "locator": locator,
            "ref": ref,
        }
        header = ("\n\n" if context else "") + f"[{source['id']}] {source['title']} · {locator}\n"
        text = text[:1600]
        truncated |= len(meta["text"]) > len(text)

        def size(length, context=context, header=header, text=text, source=source):
            return estimate_tokens(
                {"context": context + header + text[:length], "sources": sources + [source]}
            )

        length = len(text)
        if size(length) > budget:
            truncated = True
            low, high = 0, length
            while low < high:
                middle = (low + high + 1) // 2
                if size(middle) <= budget:
                    low = middle
                else:
                    high = middle - 1
            length = low
        if not text[:length].strip():
            continue
        context += header + text[:length]
        sources.append(source)
        if len(sources) == 6:
            break
    status = "no_evidence" if not sources else "partial" if truncated else "ok"
    gaps = ["上下文因预算限制截断，请缩小查询范围。"] if truncated else []
    if not sources and not gaps:
        gaps = ["当前空间未找到符合领域的已发布资料。"]
    result = {
        "status": status,
        "context": context,
        "sources": sources,
        "gaps": gaps,
        "trace_id": trace_id,
    }
    validate_contract("KnowledgeSearchResponse", result)
    return result


async def search(source, payload, *, request_id, max_bytes):
    config = source.maxkb
    space = payload.get("scope", {}).get("space_id")
    if not space or space not in config.allowed_space_ids:
        raise KnowledgeAccessDenied("maxkb_space_denied")
    published = catalog(config.catalog_path, config.catalog_sha256)
    domain = payload.get("domain", "health")
    routes = [
        route
        for route in published["routes"]
        if route.get("enabled")
        and route.get("scope_kind") == "space"
        and route.get("space_id") == space
        and "operator" in route.get("roles", [])
        and route.get("knowledge_type") in DOMAIN_TYPES[domain]
    ]
    if not routes:
        return project([], payload.get("limits", {}).get("max_context_tokens", 4000), request_id)
    try:
        async with (
            asyncio.timeout(source.timeout_seconds),
            httpx.AsyncClient(
                timeout=source.timeout_seconds, trust_env=False, follow_redirects=False
            ) as http,
        ):
            client = NativeClient(http, source, max_bytes)
            await client.login()
            prefix = "/workspace/" + config.workspace_id + "/knowledge/"
            limiter = asyncio.Semaphore(3)

            async def recall(route):
                knowledge_id = identifier(route["knowledge_id"])
                profile = published["strategies"][route["knowledge_type"]]
                if profile["mode"] not in ("embedding", "keywords", "blend"):
                    raise KnowledgeContractError("invalid_maxkb_search_mode")
                async with limiter:
                    hits = await client.call(
                        "POST",
                        prefix + knowledge_id + "/hit_test",
                        {
                            "query_text": payload["query"],
                            "top_number": 24,
                            "similarity": 0,
                            "search_mode": profile["mode"],
                        },
                    )
                if not isinstance(hits, list) or len(hits) > 24:
                    raise KnowledgeContractError("invalid_maxkb_hit_list")
                selected = []
                for hit in hits:
                    meta = published["paragraphs"].get(hit["id"])
                    if not meta or not meta.get("published"):
                        continue
                    doc = published["documents"].get(meta["document_id"])
                    if (
                        not doc
                        or not doc.get("published")
                        or "operator" not in doc.get("roles", [])
                    ):
                        continue
                    if (
                        meta["knowledge_id"] != knowledge_id
                        or hit["document_id"] != meta["document_id"]
                        or doc["knowledge_id"] != knowledge_id
                        or doc.get("space_id") != space
                        or doc.get("scope_kind") != "space"
                        or doc.get("knowledge_type") != route["knowledge_type"]
                        or hit["content"] != meta["text"]
                    ):
                        raise KnowledgeContractError("maxkb_published_evidence_changed")
                    if hit.get("is_active") is not True:
                        continue
                    score = float(hit.get("comprehensive_score") or hit.get("similarity") or 0)
                    if not math.isfinite(score):
                        raise KnowledgeContractError("invalid_maxkb_score")
                    selected.append((score, doc, {**meta, "id": hit["id"]}, hit["content"]))
                return selected

            tasks = [asyncio.create_task(recall(route)) for route in routes]
            try:
                groups = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            ranked = sorted(
                [row for group in groups for row in group], key=lambda row: (-row[0], row[2]["id"])
            )[:6]
            rows = []
            for _, doc, meta, text in ranked:
                doc_path = (
                    prefix + identifier(doc["knowledge_id"]) + "/document/" + identifier(doc["id"])
                )
                live_doc = await client.call("GET", doc_path)
                live_paragraph = await client.call(
                    "GET", doc_path + "/paragraph/" + identifier(meta["id"])
                )
                if (
                    live_doc.get("is_active") is not True
                    or live_paragraph.get("is_active") is not True
                ):
                    continue
                if live_paragraph["content"] != text:
                    raise KnowledgeContractError("maxkb_published_evidence_changed")
                rows.append((doc, meta, text))
    except TimeoutError as exc:
        raise httpx.ReadTimeout("Native MaxKB retrieval deadline exceeded") from exc
    return project(rows, payload.get("limits", {}).get("max_context_tokens", 4000), request_id)
