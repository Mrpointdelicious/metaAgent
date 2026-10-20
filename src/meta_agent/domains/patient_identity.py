"""
创建日期：2026-09-24
文件功能：识别可信患者身份并装填基本档案，供意图解析器在上下文中参考。

当前数据源为后端契约 1.6.0 的 get_multisource_patient_context；
后端 1.7.0 拆分出的 get_patient_profile 上线后切换调用端点即可，
解析逻辑与字段白名单不变（契约届时新增 name 字段，保留在上下文中，
由提示词约束不得在回答中提及）。
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 允许进入 LLM 上下文的档案字段（手机号等敏感且无价值的字段不装填）。
_FIELD_LABELS: dict[str, str] = {
    "name": "姓名",
    "sex": "性别",
    "gender": "性别",
    "age": "年龄",
    "height": "身高",
    "weight": "体重",
    "diagnosis": "诊断",
}

FACT_LABELS = _FIELD_LABELS

_NAME_RULE = "⚠ 回答不得提及患者姓名，一律以“您”称呼。"


@dataclass(frozen=True, slots=True)
class PatientBrief:
    """从可信接口解析出的患者基本档案。"""

    is_patient: bool
    facts: dict[str, Any] = field(default_factory=dict)
    display_text: str = ""
    evidence_id: str = ""
    source_version: str = ""


def brief_from_context_response(body: Any, *, include_name: bool = False) -> PatientBrief:
    """解析 get_multisource_patient_context 的响应封套。

    include_name=False 时姓名字段不装填（联调阶段默认关闭）；
    True 时姓名进入上下文并受提示词「不得提及患者姓名」约束。
    兼容 {"data": {...}} 与直接数据体两种形状；任何解析失败只降级为
    非患者档案，绝不抛出异常阻断会话。
    """
    data = (body.get("data") or body) if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return PatientBrief(is_patient=False)
    profile = data.get("profile_brief")
    binding = data.get("identity_binding")
    if not isinstance(profile, dict):
        profile = {}
    if not isinstance(binding, dict):
        binding = {}
    raw_facts = profile.get("facts")
    if not isinstance(raw_facts, dict):
        raw_facts = {}

    facts: dict[str, Any] = {}
    lines: list[str] = []
    for key, label in _FIELD_LABELS.items():
        if key == "name" and not include_name:
            continue
        value = raw_facts.get(key)
        if value is None or value == "":
            continue
        facts[key] = value
        lines.append(f"- {label}：{value}")

    is_patient = bool(
        profile.get("available") or facts or str(binding.get("binding_status") or "") == "verified"
    )
    if not is_patient:
        return PatientBrief(is_patient=False)
    if not lines:
        return PatientBrief(is_patient=True, facts=facts, display_text=_NAME_RULE)
    return PatientBrief(
        is_patient=True,
        facts=facts,
        display_text="当前患者档案（内部参考）：\n" + "\n".join(lines) + "\n" + _NAME_RULE,
    )
