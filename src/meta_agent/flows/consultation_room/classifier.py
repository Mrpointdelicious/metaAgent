"""
创建日期：2026-10-14
文件功能：从任务分类器的 LLM 输出中稳健地提取 category / reason。

兼容纯 JSON、带转义的 JSON、markdown 代码块围栏、外层 {"result": "..."} 包装、
以及混有额外文字等常见 LLM 输出；解析失败时退化为正则兜底。
"""

import contextlib
import json
import re
from typing import Any


def parse_classification(arg: Any) -> dict[str, str]:
    """提取分类器的 {category, reason}；无法解析时返回空字符串。

    分类结果只用于路由，不直接下发；内容由对应子流程决定。
    """
    if isinstance(arg, dict):
        text = json.dumps(arg, ensure_ascii=False)
    elif isinstance(arg, str):
        text = arg
    else:
        return {"category": "", "reason": ""}

    s = text.strip()
    # 去掉 markdown 代码块围栏
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)

    obj = None
    # 先尝试直接解析
    with contextlib.suppress(Exception):
        obj = json.loads(s)

    # 再尝试去掉转义后解析（LLM 输出可能带 \"）
    if obj is None:
        with contextlib.suppress(Exception):
            obj = json.loads(s.replace('\\"', '"'))

    # 若外层是 {"result": "..."} 之类的包装，解一层
    if isinstance(obj, dict):
        for key in ("result", "output", "text"):
            v = obj.get(key)
            if isinstance(v, str):
                try:
                    inner = json.loads(v)
                except Exception:
                    continue
                if isinstance(inner, dict) and "category" in inner:
                    obj = inner
                    break

    # 从 dict 取值
    if isinstance(obj, dict):
        return {
            "category": str(obj.get("category", "")),
            "reason": str(obj.get("reason", "")),
        }

    # 兜底：正则从原文提取
    category = ""
    reason = ""
    m = re.search(r'"category"\s*:\s*"([^"]*)"', s)
    if m:
        category = m.group(1)
    m = re.search(r'"reason"\s*:\s*"([^"]*)"', s)
    if m:
        reason = m.group(1)
    return {"category": category, "reason": reason}
