"""
创建日期：2026-11-16
文件功能：多疾病路径的「知识检索」环节——按「疾病 + 康复评定 / 康复建议 / 药物治疗」
三类查询取原文段落，抽出 Evaluation / Treatment / Drug / Device。

Dify 这里用的是内置知识库数据集（dataset + rerank）。本仓库对应的是
`knowledge_corpus_path` 指向的已批准语料，检索复用 domains.knowledge.rank；
语料缺失或未批准时返回空内容（下游就只建 Type 0/1 的处方，不会凭空编内容）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ASPECTS = (("康复评定", "Evaluation"), ("康复建议", "Treatment"), ("药物治疗", "Drug"))

EMPTY_CONTENT: dict[str, str] = {"Evaluation": "", "Treatment": "", "Drug": "", "Device": ""}


def _device_from_treatment(treatment: str) -> str:
    """康复建议里出现行走/坐站/平衡 → 对应设备训练方案（原节点硬编码口径）。"""
    if not treatment:
        return "暂无设备训练方案"
    if any(word in treatment for word in ("行走", "步行", "步态", "gait")):
        return "下肢机器人行走训练方案"
    if any(word in treatment for word in ("坐站", "sit-to-stand")):
        return "下肢机器人坐站训练方案"
    if any(word in treatment for word in ("平衡", "balance")):
        return "下肢机器人平衡训练方案"
    return "暂无设备训练方案"


def _load_sections(ctx: Any) -> list[tuple[Any, Any]]:
    """读取已批准的健康类语料章节；任何异常都视为「没有可用语料」。"""
    path = Path(ctx.settings.knowledge_corpus_path)
    try:
        if not path.is_file() or path.stat().st_size > 10_000_000:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        documents = payload.get("documents") or []
    except (OSError, ValueError):
        return []

    sections = []
    for doc in documents:
        if not isinstance(doc, dict) or not doc.get("approved") or not str(
            doc.get("approved_by", "")
        ).strip():
            continue
        if doc.get("domain") != "health":
            continue
        for section in doc.get("sections") or []:
            if isinstance(section, dict) and section.get("text"):
                sections.append((doc, section))
    return sections


def content_for(ctx: Any, disease: str) -> tuple[dict[str, str], str]:
    """返回 (四段内容, 来源)。来源用于 metrics：corpus / empty / dry_run。"""
    if ctx.settings.dry_run:
        return dict(EMPTY_CONTENT), "dry_run"

    sections = _load_sections(ctx)
    if not sections:
        return dict(EMPTY_CONTENT), "empty"

    from meta_agent.domains.knowledge import rank

    content = dict(EMPTY_CONTENT)
    for aspect, key in ASPECTS:
        found = rank(f"{disease}{aspect}", sections)
        if not found:
            continue
        _, index = found[0]
        text = str(sections[index][1].get("text", "")).strip()
        content[key] = text[:1000]

    if not any(content[key] for _, key in ASPECTS):
        return content, "empty"
    content["Device"] = _device_from_treatment(content["Treatment"])
    return content, "corpus"
