"""生成 MaxKB 发布目录 catalog.json。

登录 MaxKB admin API，列出已发布知识库（排除「待发布」和 Smoke 测试库），
按库名分类 knowledge_type，拉取文档和段落，组装 catalog.json 并计算 SHA-256。

用法：
  python generate_maxkb_catalog.py --username admin --password '...'
  多院区/多租户：在 maxkb/spaces.json 里维护 {space_id: [库名关键字,...]}，
  库名前缀（如「岳阳｜」「社交岛｜」）决定归属；匹配不到关键字的库会被跳过。
  没有映射文件时，全部知识库归到 --space-id（可重复传，多 space 共享同一套知识）。
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
# 默认 space（岳阳院区）。没有 space 映射文件时全部知识库都归到它。
DEFAULT_SPACE = "67ecd511-5532-405d-9b70-1cd1c0532eed"
OUT = Path(__file__).with_name("maxkb") / "catalog.json"
SPACE_MAP = Path(__file__).with_name("maxkb") / "spaces.json"

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


def load_space_map(path: Path):
    """读取 {space_id: [库名关键字, ...]}。返回 None 表示未配置映射（全部归默认 space）。"""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"space 映射文件格式错误：{path}（应为 {{space_id: [关键字,...]}}）")
    return {str(k): [str(t) for t in v] for k, v in data.items()}


def spaces_for(name: str, space_map):
    """按库名关键字判定归属；'*' 表示该 space 可访问全部知识库。"""
    hits = [
        space
        for space, tokens in space_map.items()
        if any(token == "*" or token in name for token in tokens)
    ]
    return hits


async def main(args):
    base = args.base_url.rstrip("/")
    space_map = load_space_map(Path(args.space_map)) if args.space_map else None
    # fallback：没有映射文件时，所有知识库归到 --space-id 指定的 space
    fallback_spaces = []
    for space in args.space_id or [DEFAULT_SPACE]:
        space = space.strip()
        if space and space not in fallback_spaces:
            fallback_spaces.append(space)
    if space_map:
        print(f"space 映射：{space_map}")
    else:
        print(f"未找到 space 映射文件，全部知识库归入：{fallback_spaces}")
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
            # 归属判定：有映射文件时按库名关键字精确归属，匹配不到就跳过（fail closed，
            # 防止新建库意外泄漏给别的租户）；没有映射文件时全部归默认 space。
            kb_spaces = spaces_for(name, space_map) if space_map else fallback_spaces
            if not kb_spaces:
                print(f"  跳过（无 space 归属）：{name}")
                continue
            # 一个 space 一条路由，检索时按请求的 space 精确命中，不会重复召回。
            for space in kb_spaces:
                routes.append(
                    {
                        "knowledge_id": kid,
                        "knowledge_type": ktype,
                        "enabled": True,
                        "scope_kind": "space",
                        "space_id": space,
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
                    # space_id 保留为首个归属（向后兼容旧快照校验），
                    # space_ids 是该文档对所有可见 space 的完整白名单。
                    "space_id": kb_spaces[0],
                    "space_ids": kb_spaces,
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
    print(f"routes：{len(routes)} 条（{len({route['space_id'] for route in routes})} 个 space）")
    print(f"documents：{len(documents)} 份")
    print(f"paragraphs：{len(paragraphs)} 段")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成 MaxKB 发布目录 catalog.json")
    parser.add_argument("--base-url", default="http://192.168.8.160:18081")
    parser.add_argument("--username", default=os.environ.get("MAXKB_NATIVE_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("MAXKB_NATIVE_PASSWORD", ""))
    parser.add_argument(
        "--space-map",
        default=str(SPACE_MAP),
        metavar="PATH",
        help="space 映射文件 {space_id: [库名关键字,...]}；传空字符串则忽略映射、全部归 --space-id",
    )
    parser.add_argument(
        "--space-id",
        action="append",
        default=None,
        metavar="SPACE_ID",
        help=f"没有映射文件时的归属 space，可重复传入；默认 {DEFAULT_SPACE}",
    )
    args = parser.parse_args()
    if not args.username or not args.password:
        parser.error("需要 --username / --password，或设置 MAXKB_NATIVE_USERNAME / MAXKB_NATIVE_PASSWORD")
    asyncio.run(main(args))
