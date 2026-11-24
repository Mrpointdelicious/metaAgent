"""
创建日期：2026-11-18
文件功能：AI诊室（AI问诊）真实链路移植件 —— 移植自 Dify「AI诊室(测试)」工作流代码节点。
"""

# 输入:  支持 {"sys.query": "..."}，也兼容 {"query": "..."} / {"user_input": "..."} 等
# 输出:  {"extracted": "<只返回一个值>"}

import re

TRIGGER_RE = re.compile(r"(错误码|故障码|报错|报警代码|告警代码|code|代码)", re.IGNORECASE)

def _get_text(d):
    for k in ["sys.query", "sys_query", "query", "user_input", "text", "input"]:
        if k in d and isinstance(d[k], str):
            return d[k].strip()
    return ""

def _strip_tail(s: str) -> str:
    s = s.strip().strip("。.;；!！")
    s = re.sub(r"(?:等|等等)$", "", s)
    return s.strip()

def extract_from_sys_query(q: str) -> str:
    if not q:
        return ""

    # ---------- 1) 优先：显式“触发词 + 码” ----------
    m = re.search(
        r"(?:错误码|故障码|报错|报警代码|告警代码|code|代码)\s*(?:是|为|:|：)?\s*"
        r"((?:0x[0-9A-Fa-f]+)|(?:[A-Za-z]{1,4}[-_ ]?\d{2,6})|(?:[1-9]\d{4,}))",
        q, flags=re.IGNORECASE
    )
    if m:
        return m.group(1).replace(" ", "")

    # ---------- 2) 无前缀也能识别：0x十六进制 ----------
    # 只取 0x 后 2~16 位十六进制数字，且后面不能继续跟十六进制字符（避免把“后缀”当成码的一部分）
    m = re.search(r"(?<![A-Za-z0-9])(0x[0-9A-Fa-f]{2,16})(?![0-9A-Fa-f])", q)
    if m:
        return m.group(1)

    # ---------- 3) 无前缀也能识别：纯数字长码 ----------
    # 至少 5 位；允许后面直接跟字母/下划线等后缀，但只取数字主体
    m = re.search(r"(?<![A-Za-z0-9])([1-9]\d{4,})(?!\d)", q)
    if m:
        return m.group(1)

    # ---------- 4) 带字母前缀的码（如 E101 / G-305 / ERR_23） ----------
    m = re.search(r"(?<![A-Za-z0-9_])([A-Za-z]{1,4}[-_ ]?\d{2,6})(?![A-Za-z0-9_])", q)
    if m:
        return m.group(1).replace(" ", "")

    # ---------- 5) 错误码命名 ----------
    for p in [
        r"错误码命名\s*(?:是|为|:|：)?\s*([A-Za-z][A-Za-z0-9_\-]+)",
        r"错误码命名([A-Za-z][A-Za-z0-9_\-]+)",
    ]:
        m = re.search(p, q, flags=re.IGNORECASE)
        if m:
            return m.group(1)

    # ---------- 6) IReGo 错误码提示 ----------
    for p in [
        r"IReGo错误码提示\s*(?:是|为|:|：)?\s*([^\n\r。；;]+)",
        r"IReGo错误码提示([^\n\r。；;]+)",
    ]:
        m = re.search(p, q, flags=re.IGNORECASE)
        if m:
            return _strip_tail(m.group(1))

    return ""  # 未命中


def extract_error_code(query: str) -> str:
    """从用户输入中提取错误码；未命中返回空串。"""
    q = (query or "").strip()
    if not q:
        return ""
    return extract_from_sys_query(q)
