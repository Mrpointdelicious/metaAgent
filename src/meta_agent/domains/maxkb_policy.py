"""
创建日期：2026-10-18
文件功能：在原生 MaxKB 召回前选择受授权空间和知识类型的物理知识库。
"""

from fastapi import HTTPException

STRATEGIES = {
    "scene_kb": {
        "mode": "keywords",
        "candidates": 12,
        "rerank": False,
        "name": "scene-keywords-v1",
    },
    "hospital_kb": {
        "mode": "keywords",
        "candidates": 12,
        "rerank": False,
        "name": "hospital-keywords-v1",
    },
    "sop_kb": {"mode": "blend", "candidates": 12, "rerank": False, "name": "procedure-hybrid-v1"},
    "device_kb": {
        "mode": "blend",
        "candidates": 16,
        "rerank": True,
        "name": "device-hybrid-rerank-v1",
    },
    "clinical_kb": {
        "mode": "blend",
        "candidates": 24,
        "rerank": True,
        "name": "clinical-hybrid-rerank-v1",
    },
    "rehab_kb": {
        "mode": "blend",
        "candidates": 24,
        "rerank": True,
        "name": "rehab-hybrid-rerank-v1",
    },
    "technical_kb": {
        "mode": "blend",
        "candidates": 20,
        "rerank": True,
        "name": "technical-hybrid-rerank-v1",
    },
}
DOMAIN_TYPES = {
    "help": ("sop_kb",),
    "product": ("scene_kb", "hospital_kb", "device_kb", "sop_kb", "technical_kb"),
    "health": ("clinical_kb", "rehab_kb"),
}


def resolve_types(domain, query, explicit=None):
    types = tuple(explicit or DOMAIN_TYPES[domain])
    if not explicit and domain == "product":
        selected = []
        groups = (
            (("场景", "楼", "大厅", "房间", "科室", "中心", "导航"), ("scene_kb", "hospital_kb")),
            (
                (
                    "操作",
                    "按钮",
                    "菜单",
                    "点击",
                    "登录",
                    "患者端",
                    "医生端",
                    "电脑端",
                    "跳跃",
                    "奔跑",
                ),
                ("sop_kb",),
            ),
            (("设备", "外骨骼", "仪器", "装置", "机器人"), ("device_kb",)),
            (
                ("rag", "embedding", "rerank", "算法", "机器学习", "语言模型", "向量检索"),
                ("technical_kb",),
            ),
        )
        for words, kinds in groups:
            if any(word in query.lower() for word in words):
                selected.extend(kinds)
        if selected:
            types = tuple(t for t in types if t in selected)
    if not types or any(t not in STRATEGIES for t in types):
        raise HTTPException(422, "unsupported_knowledge_type")
    return tuple(dict.fromkeys(types))


def select_routes(catalog, space_id, types):
    # Shared-space access is deliberately disabled until explicitly authorized.
    return [
        route
        for route in catalog["routes"]
        if route.get("enabled")
        and route.get("knowledge_type") in types
        and route.get("scope_kind") == "space"
        and route.get("space_id") == space_id
        and "operator" in route.get("roles", [])
    ]


def allowed_document(doc, route, space_id):
    return (
        doc.get("published")
        and doc.get("knowledge_id") == route["knowledge_id"]
        and doc.get("scope_kind") == "space"
        and doc.get("space_id") == space_id
        and doc.get("knowledge_type") == route["knowledge_type"]
        and "operator" in doc.get("roles", [])
    )
