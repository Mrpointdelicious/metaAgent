"""
创建日期：2026-10-02
文件功能：提供符合 IReTour 契约的合成演示数据，不生成虚构报表图片。
"""


def iretour_demo_response(endpoint, payload):
    body = {
        "tool_name": endpoint,
        "contract_version": "iretour-1.0.0",
        "request_id": "demo",
        "status": "success",
        "reason_codes": [],
        "patient_message": "这些是合成演示数据。",
        "meta": {"source_completeness": "demo"},
    }
    ref = payload.get("session_ref") or "synthetic-tour-1"
    when = "2026-09-01T10:00:00+08:00"
    activity = {"canonical_code": "straight_primary", "display_name": "合成直线初级训练"}
    if endpoint == "get_iretour_patient_context":
        body["data"] = {
            "i_re_tour": {
                "availability": "available",
                "counts": {"session_count": 4, "usable_session_count": 4},
                "quality": {"usable": True, "issues": []},
            }
        }
    elif endpoint == "get_iretour_patient_history":
        page = payload.get("page_number", 1)
        body["data"] = {
            "page": {"page_number": page, "has_next": False},
            "items": [
                {
                    "session_ref": f"synthetic-tour-{i}",
                    "session_time": when,
                    "training_state": "completed",
                    "result_state": "summary_available",
                    "activity": activity,
                }
                for i in range(1, 5)
            ]
            if page == 1
            else [],
        }
    elif endpoint == "get_iretour_session_analysis":
        body["data"] = {
            "session": {"session_ref": ref},
            "time": {"execution_ended_at": when, "training_state": "completed"},
            "activity": activity,
            "plan": {"duration_seconds": 120},
            "result": {
                "result_state": "summary_available",
                "metrics": [
                    {
                        "metric_code": "speed",
                        "display_name": "速度",
                        "raw_value": 0.6,
                        "normalized_value": 0.6,
                        "unit": "m/s",
                        "value_status": "valid",
                    }
                ],
            },
            "quality": {"usable": True, "issues": []},
        }
    elif endpoint == "get_iretour_longitudinal_analysis":
        count = payload.get("report_count", 4)
        body["data"] = {
            "window": {
                "started_at": when,
                "ended_at": when,
                "requested_count": count,
                "resolved_count": count,
                "comparable_count": count,
                "is_contiguous": True,
            },
            "metric_series": [
                {
                    "metric_code": "speed",
                    "display_name": "速度",
                    "unit": "m/s",
                    "direction": "increased",
                    "interpretation_status": "not_evaluated",
                    "points": [
                        {
                            "ordinal": i,
                            "session_ref": f"synthetic-tour-{i}",
                            "value": i * 0.1,
                            "value_status": "valid",
                        }
                        for i in range(1, count + 1)
                    ],
                }
            ],
        }
    else:
        body.update(status="unavailable", data=None, patient_message="演示模式不生成真实图片。")
    return body
