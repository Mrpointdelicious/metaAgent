"""
创建日期：2026-10-27
更新：2026-11-20 改为实时检索（live）模式。
文件功能：直接调用 MaxKB 原生 API 检索已发布资料，保留空间引用边界。

live 模式要点：
  - 不依赖本地 catalog 快照；每次检索实时登录、实时列库、实时 hit_test。
  - 知识库名首段为 space 归属标志：
      <spaceid>  → 仅该 space 可见（专属）
      global     → 所有 space 可见（共用）
    多个归属可用逗号分隔。
  - 知识内容在 MaxKB 更新后即时可查，无需重新生成快照。
  - 安全边界：allowed_space_ids 白名单仍然强制校验；角色、域、类型仍按库名解析。
"""
import asyncio
import hashlib
import json
import math
import re
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
    # help 域纳入 scene_kb：楼层/科室/场景介绍只发布在场景库，而模型对
    # 「介绍X楼」这类问题常选 help；不纳入则该类内容永远检索不到。
    "help": {"sop_kb", "scene_kb"},
    "product": {"scene_kb", "hospital_kb", "device_kb", "sop_kb", "technical_kb"},
}

# 库名类型片段 → knowledge_type（与 generate_maxkb_catalog.py 的 TYPE_MAP 保持一致）
TYPE_MAP = {
    "设备资料": "device_kb",
    "技术研究参考": "technical_kb",
    "操作说明": "sop_kb",
    "场景说明": "scene_kb",
    "医院流程": "hospital_kb",
    "临床文献": "clinical_kb",
    "康复文献": "rehab_kb",
}
GLOBAL_OWNER = "global"
SEARCH_MODE = "blend"
SKIP_PREFIXES = ("待发布", "Smoke")


def identifier(value):
    return str(UUID(str(value)))


def resolve_owner(token: str, aliases: dict[str, str]) -> str:
    """把库名首段的归属 token 解析为完整 spaceid；global 原样保留。"""
    if token == GLOBAL_OWNER:
        return token
    return aliases.get(token, token)


def parse_library(name: str):
    """从库名解析 (owners, knowledge_type, roles)。

    库名格式（全角竖线分隔）：<归属标志>｜<类型>｜<载体>｜<角色>
      归属标志：spaceid 或短标志（别名）或 global，多个用逗号/中文逗号分隔。
    返回 None 表示库名不符合规范，跳过（fail closed）。
    """
    parts = [p.strip() for p in name.split("｜") if p.strip()]
    if len(parts) < 2:
        return None
    owners = [x.strip() for x in re.split(r"[,，]", parts[0]) if x.strip()]
    if not owners:
        return None
    ktype = next((t for seg, t in TYPE_MAP.items() if seg in name), None)
    if not ktype:
        return None
    roles = [r.strip() for r in parts[-1].split("/") if r.strip()]
    return owners, ktype, roles


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


async def search(source, payload, *, request_id, max_bytes, catalog_role=None):
    """实时检索：每次登录、列库、hit_test 均走 MaxKB，不读本地快照。

    ``catalog_role`` 由 dev 角色授权链传入；未传时保持 main live 的
    operator 可见性兼容口径。
    """
    config = source.maxkb
    space = payload.get("scope", {}).get("space_id")
    if not space or space not in config.allowed_space_ids:
        raise KnowledgeAccessDenied("maxkb_space_denied")
    domain = payload.get("domain", "health")
    budget = payload.get("limits", {}).get("max_context_tokens", 4000)
    try:
        async with (
            asyncio.timeout(source.timeout_seconds),
            httpx.AsyncClient(
                timeout=source.timeout_seconds, trust_env=False, follow_redirects=False
            ) as http,
        ):
            client = NativeClient(http, source, max_bytes)
            await client.login()
            prefix = "/workspace/" + config.workspace_id + "/knowledge"

            # 1) 实时列库，按库名首段的 space 标志 + 域 + 角色过滤候选库
            libs = await client.call("GET", prefix)
            if not isinstance(libs, list):
                raise KnowledgeContractError("invalid_maxkb_library_list")
            candidates = []
            for lib in libs:
                if not isinstance(lib, dict):
                    continue
                name = lib.get("name", "")
                if name.startswith(SKIP_PREFIXES):
                    continue
                parsed = parse_library(name)
                if not parsed:
                    continue
                owners, ktype, roles = parsed
                resolved = [resolve_owner(o, config.owner_aliases) for o in owners]
                if GLOBAL_OWNER not in resolved and space not in resolved:
                    continue
                required_role = catalog_role or "operator"
                if required_role not in roles:
                    continue
                if ktype not in DOMAIN_TYPES[domain]:
                    continue
                candidates.append((lib["id"], ktype))
            if not candidates:
                return project([], budget, request_id)

            # 2) 并发 hit_test；直接用 API 返回的正文与元数据构造结果
            limiter = asyncio.Semaphore(3)

            async def recall(knowledge_id):
                kid = identifier(knowledge_id)
                async with limiter:
                    hits = await client.call(
                        "POST",
                        prefix + "/" + kid + "/hit_test",
                        {
                            "query_text": payload["query"],
                            "top_number": 24,
                            "similarity": 0,
                            "search_mode": SEARCH_MODE,
                        },
                    )
                if not isinstance(hits, list) or len(hits) > 24:
                    raise KnowledgeContractError("invalid_maxkb_hit_list")
                selected = []
                for hit in hits:
                    if not isinstance(hit, dict) or hit.get("is_active") is not True:
                        continue
                    if hit.get("knowledge_id") != knowledge_id:
                        continue
                    content = hit.get("content") or ""
                    if not content.strip():
                        continue
                    document_id = hit.get("document_id")
                    hit_id = hit.get("id")
                    if not document_id or not hit_id:
                        continue
                    score = float(hit.get("comprehensive_score") or hit.get("similarity") or 0)
                    if not math.isfinite(score):
                        raise KnowledgeContractError("invalid_maxkb_score")
                    document_name = hit.get("document_name") or ""
                    match = re.search(r"__([0-9a-f]{6,})__", document_name)
                    code = match.group(1) if match else identifier(document_id).replace("-", "")[:8]
                    # 版本标识用正文哈希：内容更新后引用随之变化，可追溯。
                    revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    doc = {"code": code, "revision_sha256": revision, "name": document_name}
                    meta = {
                        "id": hit_id,
                        "title": hit.get("title") or "",
                        "page": None,
                        "text": content,
                    }
                    selected.append((score, doc, meta, content))
                return selected

            tasks = [asyncio.create_task(recall(kid)) for kid, _ in candidates]
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
            rows = [(doc, meta, text) for _, doc, meta, text in ranked]
    except TimeoutError as exc:
        raise httpx.ReadTimeout("Native MaxKB live retrieval deadline exceeded") from exc
    return project(rows, budget, request_id)
