"""
创建日期：2026-09-08
文件功能：提供明确标记的合成开发数据，不伪造真实动作或可访问报表。
"""

from typing import Any

from meta_agent.contracts import utcnow


def demo_response(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    if "iretour" in endpoint:
        from meta_agent.tools.demo_iretour import iretour_demo_response

        return iretour_demo_response(endpoint, payload)
    if endpoint == "query_hospital_operations":
        return {
            "tool_name": endpoint,
            "contract_version": "1.2.0",
            "request_id": "demo",
            "status": "success",
            "data": {
                "period": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
                "hospitals": [
                    {
                        "hospital": {"hospital_name": "合成演示机构"},
                        "overview": {"patient_count": 10, "doctor_count": 2},
                    }
                ],
                "analysis": [{"text": "以上为合成演示数据。"}],
                "report": None,
            },
        }
    if endpoint == "search_doctors":
        return {
            "status": 200,
            "data": {
                "count": 0,
                "doctorNames": "",
                "doctorIds": "0",
                "doctorPhones": "",
                "command": "",
            },
        }
    if endpoint == "navigate_scene":
        return {
            "status": 200,
            "data": {
                "spaceId": payload.get("spaceId"),
                "version": 0,
                "commands": [
                    {
                        "order": 1,
                        "sourceText": payload.get("target", ""),
                        "status": "unmatched",
                        "result": None,
                        "message": "演示模式不返回真实场景动作。",
                        "candidates": [],
                    }
                ],
            },
        }
    body: dict[str, Any] = {
        "tool_name": endpoint,
        "contract_version": "1.6.0",
        "request_id": "demo",
        "status": "success",
        "reason_codes": [],
        "patient_message": None,
        "meta": {
            "generated_at": utcnow().isoformat(),
            "watermark": "synthetic-v1",
            "source_completeness": "demo",
        },
    }
    if endpoint == "get_multisource_patient_context":
        body["data"] = {
            "profile_brief": {"available": True, "facts": {"age": 65, "sex": "女"}},
            "data_domains": {
                "irego": {
                    "availability": "available",
                    "counts": {"report_count": 1},
                    "quality": {"patient_message": "这些是演示数据。"},
                }
            },
            "snapshot": {},
        }
    elif endpoint == "get_irego_patient_history":
        body["data"] = {
            "page": {"page_number": payload.get("page_number", 1), "has_next": False},
            "summary": {"report_count": 1},
            "items": [
                {
                    "ordinal": 1,
                    "record_type": "report",
                    "session_ref": "synthetic-session",
                    "plan_ref": "synthetic-plan",
                    "session_time": "2026-09-01T10:00:00+08:00",
                    "training_state": "completed",
                    "report_status": "complete",
                    "display_names": ["合成步行训练"],
                    "patient_message": "演示训练已完成。",
                }
            ],
        }
    elif endpoint == "get_irego_session_analysis":
        body["data"] = {
            "session": {"session_ref": payload.get("session_ref") or "synthetic-session"},
            "time": {"training_state": "completed", "completed_at": "2026-09-01T10:00:00+08:00"},
            "overview": {
                "total_training_duration": 120,
                "duration_unit": "s",
                "report_block_count": 1,
            },
            "report_blocks": [
                {
                    "display_name": "合成步行训练",
                    "rehab_content": "walking",
                    "metrics": [
                        {
                            "metric_code": "walking_speed",
                            "display_name": "步行速度",
                            "normalized_value": 0.5,
                            "value_status": "valid",
                            "unit": "m/s",
                            "unit_status": "confirmed",
                        }
                    ],
                }
            ],
            "report_evaluation_facts": {"completion_rate": 1.0},
            "quality": {
                "overall_status": "valid",
                "trend_eligibility": "ineligible",
                "explanation_boundary": "合成演示记录，不代表真实患者训练表现。",
            },
        }
    elif endpoint == "get_patient_profile":
        body["contract_version"] = "patient-layers-1.0.0"
        body["data"] = {
            "patient_ref": "project:" + str(payload.get("system_context", {}).get("user", "3799")),
            "profile": {
                "gender": "女",
                "age": 65,
                "self_reported_medical_history": "合成示例：左膝疼痛",
            },
            "source": "synthetic_demo",
            "limitations": ["合成演示资料，不代表真实患者。"],
        }
    elif endpoint == "get_patient_consultation":
        body["contract_version"] = "patient-layers-1.0.0"
        body["data"] = {
            "patient_ref": "project:" + str(payload.get("system_context", {}).get("user", "3799")),
            "medical_record_count": 1,
            "consultation_count": 0,
            "page": {"page_number": 1, "page_size": 5, "total_items": 1, "has_next": False},
            "medical_records": [
                {
                    "record_id": 1,
                    "recorded_at": "2025-01-01T10:00:00+08:00",
                    "content": {
                        "diagnosis": "合成示例：膝关节疼痛",
                        "doctor_orders": "合成示例：按既定康复计划训练，记录训练后的感受。",
                    },
                }
            ],
            "consultations": [],
            "limitations": ["合成演示資料，不代表真实患者。"],
        }
    elif endpoint == "get_patient_rehab":
        body["contract_version"] = "patient-layers-1.0.0"
        body["data"] = {
            "patient_ref": "project:" + str(payload.get("system_context", {}).get("user", "3799")),
            "plan_count": 0,
            "plans": [],
            "iretour": {
                "availability": "success",
                "history": demo_response("get_iretour_patient_history", payload)["data"],
            },
            "irego": {"availability": "unavailable", "patient_message": "合成示例没有IReGo映射。"},
            "limitations": ["合成演示资料，不代表真实患者。"],
        }
    else:
        body.update(
            status="unavailable", data=None, patient_message="演示模式不提供该真实业务结果。"
        )
    return body
