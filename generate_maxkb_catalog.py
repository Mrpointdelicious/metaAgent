"""生成 MaxKB 发布目录 catalog.json。

登录 MaxKB admin API，列出已发布知识库（排除「待发布」和 Smoke 测试库），
按库名分类 knowledge_type，拉取文档和段落，组装 catalog.json 并计算 SHA-256。

用法：
  python generate_maxkb_catalog.py --username admin --password '...'
  或设置环境变量 MAXKB_NATIVE_USERNAME / MAXKB_NATIVE_PASSWORD 后直接运行。
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

import httpx

WORKSPACE = "default"
OUT = Path(__file__).with_name("maxkb") / "catalog.json"

# 库名类型片段 → knowledge_type
TYPE_MAP = {
    "设备资料": "device_kb",
    "技术研究参考": "technical_kb",
    "操作说明": "sop_kb",
    "场景说明": "scene_kb",
    "医院流程": "hospital_kb",
    "临床文献": "clinical_kb",
    "康复文献": "rehab_kb",
}
# 检索模式（可后调）：文献/操作/场景统一用 blend 混合检索
STRATEGIES = {k: {"mode": "blend"} for k in TYPE_MAP.values()}


def classify(name: str):
    for seg, ktype in TYPE_MAP.items():
        if seg in name:
            return ktype
    return None


def roles_of(name: str):
    parts = [p.strip() for p in name.split("｜") if p.strip()]
    if not parts:
        return []
    last = parts[-1]
    return [r.strip() for r in last.split("/") if r.strip()]


async def main(args):
    base = args.base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
        r = await client.post(
            f"{base}/admin/api/user/login",
            json={"username": args.username, "password": args.password},
        )
        r.raise_for_status()
        token = r.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        kb_base = f"{base}/admin/api/workspace/{WORKSPACE}/knowledge"

        r = await client.get(kb_base, headers=headers)
        kbs = r.json()["data"]

        routes, documents, paragraphs = [], {}, {}
        for kb in kbs:
            name = kb["name"]
            if name.startswith("待发布") or name.startswith("Smoke"):
                continue
            ktype = classify(name)
            if not ktype:
                print(f"  跳过（无法分类）：{name}")
                continue
            roles = roles_of(name)
            kid = kb["id"]
            routes.append(
                {
                    "knowledge_id": kid,
                    "knowledge_type": ktype,
                    "enabled": True,
                    "scope_kind": "space",
                    "space_id": args.space_id,
                    "roles": roles,
                }
            )
            rd = await client.get(f"{kb_base}/{kid}/document", headers=headers)
            docs = rd.json()["data"] if isinstance(rd.json().get("data"), list) else []
            for doc in docs:
                if not isinstance(doc, dict) or doc.get("is_active") is not True:
                    continue
                did = doc["id"]
                rp = await client.get(f"{kb_base}/{kid}/document/{did}/paragraph", headers=headers)
                paras = rp.json()["data"] if isinstance(rp.json().get("data"), list) else []
                texts, para_entries = [], {}
                for para in paras:
                    if not isinstance(para, dict) or para.get("is_active") is not True:
                        continue
                    content = para.get("content") or para.get("text") or ""
                    if not content.strip():
                        continue
                    pid = para["id"]
                    para_entries[pid] = {
                        "document_id": did,
                        "knowledge_id": kid,
                        "published": True,
                        "text": content,
                        "title": para.get("title") or "",
                        "page": para.get("page") if isinstance(para.get("page"), int) else None,
                    }
                    texts.append(content)
                if not para_entries:
                    continue
                # code：文档名里的 __hash 段，没有就用文档 UUID 前 8 位
                m = re.search(r"__([0-9a-f]{6,})__", doc.get("name", ""))
                code = m.group(1) if m else did[:8]
                documents[did] = {
                    "id": did,
                    "knowledge_id": kid,
                    "published": True,
                    "roles": roles,
                    "scope_kind": "space",
                    "space_id": args.space_id,
                    "knowledge_type": ktype,
                    "code": code,
                    "name": doc.get("name", ""),
                    "revision_sha256": hashlib.sha256("".join(texts).encode()).hexdigest(),
                }
                paragraphs.update(para_entries)
            print(f"  {name}  ->  {ktype}  文档{len([d for d in documents.values() if d['knowledge_id']==kid])} 段")

    catalog = {
        "schema_version": 2,
        "routes": routes,
        "strategies": STRATEGIES,
        "documents": documents,
        "paragraphs": paragraphs,
    }
    raw = json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    sha = hashlib.sha256(raw).hexdigest()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(raw)
    print(f"\n=== 生成完成 ===")
    print(f"文件：{OUT}")
    print(f"catalog_sha256：{sha}")
    print(f"routes：{len(routes)} 个知识库")
    print(f"documents：{len(documents)} 份")
    print(f"paragraphs：{len(paragraphs)} 段")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成 MaxKB 发布目录 catalog.json")
    parser.add_argument("--base-url", default="http://192.168.8.160:18081")
    parser.add_argument("--username", default=os.environ.get("MAXKB_NATIVE_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("MAXKB_NATIVE_PASSWORD", ""))
    parser.add_argument("--space-id", default="67ecd511-5532-405d-9b70-1cd1c0532eed")
    args = parser.parse_args()
    if not args.username or not args.password:
        parser.error("需要 --username / --password，或设置 MAXKB_NATIVE_USERNAME / MAXKB_NATIVE_PASSWORD")
    asyncio.run(main(args))
