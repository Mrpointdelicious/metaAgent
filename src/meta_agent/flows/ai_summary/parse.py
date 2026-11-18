"""
创建日期：2026-11-16
文件功能：把 LLM 的纪要文本拆成 part / all 两段（对齐 Dify 工作流的后处理代码节点）。

Dify 侧返回 {{"summary": "<json 字符串>"}}，其中 json 为
{{"part": 部分对话总结, "all": 全部对话总结}}；这里保持同一契约。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .summary_prompt import ALL_TAG, PART_TAG


def parse_summary(payload: Any) -> dict[str, str]:
    """从模型输出里取出两段总结；缺哪个就留空，不抛异常。"""
    text = payload
    if isinstance(payload, dict):
        text = payload.get("dialogue", "")
    elif isinstance(payload, str):
        try:
            obj = json.loads(payload)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "dialogue" in obj:
            text = obj.get("dialogue", "")
    if not isinstance(text, str):
        text = ""
    text = text.strip()

    part_text = ""
    all_text = ""
    if PART_TAG in text and ALL_TAG in text:
        part_start = text.find(PART_TAG) + len(PART_TAG)
        all_start = text.find(ALL_TAG)
        part_text = text[part_start:all_start].strip()
        all_text = text[all_start + len(ALL_TAG) :].strip()
    elif PART_TAG in text:
        part_text = text[text.find(PART_TAG) + len(PART_TAG) :].strip()
    elif ALL_TAG in text:
        all_text = text[text.find(ALL_TAG) + len(ALL_TAG) :].strip()

    # Dify 原逻辑：两段结尾都补一个换行
    if part_text:
        part_text += "\n"
    if all_text:
        all_text += "\n"
    return {"part": part_text, "all": all_text}


def render_summary(summary: dict[str, str]) -> str:
    """序列化成对外的 summary 字符串（中文不转义）。"""
    return json.dumps(summary, ensure_ascii=False)


# 没有真实对话记录时，模型会给不出时间区间就写 00:00:00，属于噪音。
_ZERO_TIME_RANGE = re.compile(r"[（(]?\s*起止时间：00:00:00-00:00:00\s*[）)]?")

# 模型输出的任意时间区间（可能不是真实时间），统一替换成记录里的真实区间。
_ANY_TIME_RANGE = re.compile(
    r"[（(]\s*起止时间：\s*\d{1,2}:\d{2}:\d{2}\s*[-—~]\s*\d{1,2}:\d{2}:\d{2}\s*[）)]"
)


def strip_zero_time_range(text: str) -> str:
    """去掉占位用的 00:00:00 时间区间，并收敛多余空行。"""
    cleaned = _ZERO_TIME_RANGE.sub("", text)
    return re.sub(r"\n{2,}", "\n", cleaned)


def apply_time_range(part_text: str, span: str) -> str:
    """把每条小结的「起止时间」改成这批记录的真实区间。

    模型看不到稳定时钟（或者干脆照抄 00:00:00），而请求里本来就带着 timeStamp，
    所以以记录为准：已有区间就替换，整段没写就补在每条 `[发言人]：…` 的句末。
    """
    if not part_text or not span:
        return part_text
    marker = f"（起止时间：{span}）"
    lines: list[str] = []
    for line in part_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ANY_TIME_RANGE.search(stripped):
            stripped = _ANY_TIME_RANGE.sub(marker, stripped)
        elif stripped.startswith("["):
            stripped = f"{stripped}{marker}"
        lines.append(stripped)
    if not lines:
        return part_text
    return "\n".join(lines) + ("\n" if part_text.endswith("\n") else "")
