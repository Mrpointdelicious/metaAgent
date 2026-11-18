"""
创建日期：2026-11-16
文件功能：验证处方推荐的病种提取（对齐 Dify「找不到就取最相近病名」的兜底语义）。
"""

import json

from meta_agent.flows.consultation_room.prescription import (
    PRESCRIPTION_INDICATIONS,
    parse_disease_recommendation,
)


def test_all_sources_empty_returns_empty():
    assert parse_disease_recommendation("", "", "", "", answer="") == {"disease": ""}


def test_model_answer_aligned_to_glossary():
    result = parse_disease_recommendation("", "", "推荐处方", "", answer="脑卒中")
    assert result["disease"] == "脑卒中"


def test_doctor_query_substring_wins_when_answer_unusable():
    result = parse_disease_recommendation(
        "", "", "给患者推荐一份脑卒中的处方", "", answer="不知道"
    )
    assert result["disease"] == "脑卒中"


def test_fallback_guarantees_disease_when_source_present():
    # 关键回归：对话里只有「用户不在线」这类无病种内容时，Dify 原逻辑仍会挑最相近词，
    # 不会返回空、也就不会触发「请明确本次需要推荐的病种或评估目的」。
    conversation = json.dumps({"code": 200, "message": "用户不在线", "data": []})
    result = parse_disease_recommendation("", conversation, "推荐处方", "", answer="")
    assert result["disease"]
    assert result["disease"] in PRESCRIPTION_INDICATIONS


def test_medical_history_is_used_when_other_sources_missing():
    medical = json.dumps(
        {"data": {"PatientDetails": {"MedicalHistory": "脑卒中后遗症，高血压"}}}
    )
    result = parse_disease_recommendation(medical, "", "", "", answer="")
    assert result["disease"] == "脑卒中后遗症"


def test_re_recommend_reuses_last_disease():
    result = parse_disease_recommendation("", "", "重新推荐", "脊髓损伤", answer="")
    assert result["disease"] == "脊髓损伤"
