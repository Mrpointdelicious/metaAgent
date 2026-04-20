from __future__ import annotations

import re


PATIENT_ROSTER_EXPLICIT_QUERY_KEYWORDS = (
    "我的患者",
    "我的病人",
    "所有患者",
    "所有的患者",
    "所有病人",
    "所有的病人",
    "患者名单",
    "病人名单",
    "my patients",
    "all my patients",
    "patient list",
)
PATIENT_ROSTER_SUBJECT_KEYWORDS = PATIENT_ROSTER_EXPLICIT_QUERY_KEYWORDS + (
    "患者",
    "病人",
    "patients",
    "patient",
)
DOCTOR_ROSTER_QUERY_KEYWORDS = (
    "我的医生",
    "相关医生",
    "相关的医生",
    "有关医生",
    "有关的医生",
    "医生名单",
    "list my doctors",
    "my doctors",
)
ROSTER_ACTION_KEYWORDS = (
    "列出",
    "查询",
    "查看",
    "查找",
    "搜索",
    "找出",
    "显示",
    "有哪些",
    "list",
    "show",
    "find",
    "search",
)
PATIENT_VISIT_SEMANTIC_KEYWORDS = (
    "就诊",
    "来访",
    "来过",
    "到训",
    "康复过",
    "来康复过",
    "最近来过",
    "最近来访",
    "最近就诊",
    "visited",
    "attended",
    "rehab",
)
PATIENT_RESULT_SET_FOLLOWUP_REFERENCES = (
    "这些患者",
    "以上这些患者",
    "上面这些患者",
    "这些病人",
    "上面那批人",
    "刚才那批患者",
    "刚才那批病人",
    "这批患者",
    "这批病人",
    "他们",
    "她们",
    "them",
    "previous patients",
)


def has_patient_roster_subject(text: str) -> bool:
    return _has_any(text, PATIENT_ROSTER_SUBJECT_KEYWORDS)


def has_patient_visit_semantics(text: str) -> bool:
    return _has_any(text, PATIENT_VISIT_SEMANTIC_KEYWORDS)


def has_patient_roster_query(text: str) -> bool:
    if _has_any(text, PATIENT_ROSTER_EXPLICIT_QUERY_KEYWORDS):
        return True
    return has_patient_roster_subject(text) and (_has_roster_action(text) or has_patient_visit_semantics(text))


def has_patient_roster_seed_query(text: str) -> bool:
    return has_patient_roster_query(text) and not has_patient_result_set_followup_reference(text)


def has_patient_result_set_followup_reference(text: str) -> bool:
    return _has_any(text, PATIENT_RESULT_SET_FOLLOWUP_REFERENCES)


def has_doctor_roster_query(text: str) -> bool:
    return _has_any(text, DOCTOR_ROSTER_QUERY_KEYWORDS) and _has_roster_action(text)


def extract_roster_days(text: str) -> int | None:
    lowered = text.lower()
    if any(token in text for token in ("本周", "最近一周", "近一周")) or "last week" in lowered:
        return 7
    if any(token in text for token in ("本月", "最近一个月", "近一个月")) or "last month" in lowered:
        return 30
    for pattern in (
        r"(?:最近|过去|近|当前|这)\s*(\d+)\s*天",
        r"(\d+)\s*天\s*(?:以来|以内|之内|内|来)?",
        r"last\s*(\d+)\s*days?",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def extract_roster_limit(text: str) -> int | None:
    for pattern in (
        r"(?:只\s*)?(?:显示|列出|展示)?\s*前\s*(\d+)\s*(?:个|位|名|条)?",
        r"top\s*(\d+)",
        r"limit\s*(\d+)",
        r"first\s*(\d+)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def extract_limit(text: str) -> int | None:
    return extract_roster_limit(text)


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in keywords)


def _has_roster_action(text: str) -> bool:
    return _has_any(text, ROSTER_ACTION_KEYWORDS)
