"""
创建日期：2026-11-16
文件功能：AI医嘱生成的输入规整——把记录数组转成「谁说了什么」的对话文本。

对齐 Dify 工作流里的第一个代码节点：
    [{"role":"Patient","timeStamp":…,"from":"医生1","message":"有吗？"}, …]
      → 医生(医生1)：有吗？
        患者(医生1)：你好，你好你好。

注意：`role` 决定「医生/患者」，`from` 是发言人名字，两者可能对不上
（示例里 role=Patient、from=医生1），这里按 Dify 原逻辑分开渲染，不做纠正。
"""

from __future__ import annotations

import json
from typing import Any


def _role_label(raw: Any) -> str:
    role = str(raw or "").strip().lower()
    if "doctor" in role:
        return "医生"
    if "patient" in role:
        return "患者"
    return "未知"


def conversation_text(sys_query: Any) -> str:
    """把 query 里的记录数组拼成对话文本；不是数组就按普通文本原样返回。

    与 Dify 代码节点保持一致：
      - 空输入 / "[]" → 空串；
      - 不是 JSON → 原文（去空白）；
      - 是 JSON 但不是数组 → 空串；
      - 元素解析不了 / 没有 message → 跳过该条。
    """
    text = str(sys_query or "").strip()
    if not text or text == "[]":
        return ""

    try:
        payload = json.loads(text)
    except ValueError:
        return text

    if not isinstance(payload, list):
        return ""

    lines: list[str] = []
    for item in payload:
        record: Any = item
        if isinstance(item, str):
            try:
                record = json.loads(item)
            except ValueError:
                continue
        if not isinstance(record, dict):
            continue
        name = str(record.get("from") or "未知").strip()
        message = str(record.get("message") or "").strip()
        if not message:
            continue
        lines.append(f"{_role_label(record.get('role'))}({name})：{message}")
    return "\n".join(lines).strip()


def has_dialogue_roles(text: str) -> bool:
    """对话里是否出现了医生或患者——都没有就按「无问诊记录」处理。"""
    return "医生" in text or "患者" in text


# 语言层面的引号统一：Dify 后处理代码节点的作用（中文/单引号 → 英文双引号）。
_QUOTE_MAP = {
    "\u201c": '"',  # “
    "\u201d": '"',  # ”
    "\u2018": '"',  # ‘
    "\u2019": '"',  # ’
    "'": '"',
}


def normalize_quotes(text: str) -> str:
    """把各种引号统一成英文双引号，保证下游能直接 json.loads。"""
    for source, target in _QUOTE_MAP.items():
        text = text.replace(source, target)
    return text


def strip_code_fence(text: str) -> str:
    """去掉模型偶尔加的 ```json 围栏（Dify 没有这一步，属于容错）。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# 结构位上的中文标点：模型照抄中文习惯时会出现「，」「：」，直接 json.loads 会失败。
# 只替换字符串字面量之外的字符，正文里的中文标点原样保留。
_STRUCTURAL_MAP = {
    "\uff0c": ",",  # ，
    "\uff1a": ":",  # ：
    "\uff1b": ";",  # ；
    "\uff5b": "{",  # ｛
    "\uff5d": "}",  # ｝
    "\uff3b": "[",  # ［
    "\uff3d": "]",  # ］
}


def repair_structural_punct(text: str) -> str:
    """把字符串外的中文标点换成英文，尽量让模型输出能被 json.loads 解析。"""
    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
            continue
        out.append(_STRUCTURAL_MAP.get(char, char))
    return "".join(out)


def is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def to_json_text(raw: str) -> str:
    """后处理：去围栏 → 统一引号 → 必要时修结构位标点，产出可解析的 JSON 文本。"""
    text = normalize_quotes(strip_code_fence(raw.strip()))
    if is_json(text):
        return text
    repaired = repair_structural_punct(text)
    return repaired if is_json(repaired) else text


# 医嘱 JSON 的三个字段（顺序即输出顺序）。
ORDER_KEYS = ("ChiefComplaint", "Diagnosis", "Management")


def build_order_json(raw: str) -> tuple[str, bool]:
    """把模型输出规整成纯医嘱 JSON 对象文本，返回 (文本, 是否解析成功)。

    能解析成对象时按固定键序重排、缺的键补空串（多余键保留在后面），
    让下游拿到稳定的三个字段；解析不了就原样返回后处理过的文本。
    """
    text = to_json_text(raw)
    try:
        payload = json.loads(text)
    except ValueError:
        return text, False
    if not isinstance(payload, dict):
        return text, False

    ordered: dict[str, Any] = {}
    for key in ORDER_KEYS:
        value = payload.get(key)
        ordered[key] = "" if value is None else value
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    return json.dumps(ordered, ensure_ascii=False), True
