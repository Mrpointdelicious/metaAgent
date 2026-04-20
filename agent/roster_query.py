from __future__ import annotations

import re


PATIENT_ROSTER_QUERY_KEYWORDS = (
    "我的患者",
    "我的病人",
    "所有患者",
    "所有的患者",
    "所有病人",
    "所有的病人",
    "患者名单",
    "病人名单",
    "list my patients",
    "my patients",
    "all my patients",
)
DOCTOR_ROSTER_QUERY_KEYWORDS = (
    "我的医生",
    "相关医生",
    "有关医生",
    "医生名单",
    "list my doctors",
    "my doctors",
)
ROSTER_ACTION_KEYWORDS = (
    "列出",
    "查询",
    "查看",
    "名单",
    "有哪些",
    "list",
    "show",
    "all",
)


def has_patient_roster_query(text: str) -> bool:
    return has_patient_roster_subject(text) and _has_roster_action(text)


def has_doctor_roster_query(text: str) -> bool:
    return _has_subject(text, DOCTOR_ROSTER_QUERY_KEYWORDS) and _has_roster_action(text)


def has_patient_roster_subject(text: str) -> bool:
    return _has_subject(text, PATIENT_ROSTER_QUERY_KEYWORDS)


def extract_limit(text: str) -> int | None:
    lowered = text.lower()
    for pattern in (
        r"(?:前|只显示前|显示前|top)\s*(\d+)",
        r"limit\s*(\d+)",
        r"first\s*(\d+)",
    ):
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _has_subject(text: str, subject_keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in subject_keywords)


def _has_roster_action(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in ROSTER_ACTION_KEYWORDS)
