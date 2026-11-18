"""
创建日期：2026-11-16
文件功能：验证 AI纪要总结的文本前处理与后处理（对齐 Dify 两个代码节点 + 拆分节点）。
"""

import json

from meta_agent.flows.ai_summary.parse import parse_summary, render_summary
from meta_agent.flows.ai_summary.textproc import (
    convert_text_time,
    convert_ts,
    integrate,
    is_semantic_valid,
    normalize_records,
    process_text_times,
    remove_trailing_meaningless,
)


def test_convert_ts_handles_seconds_and_milliseconds():
    # 2023-11-14 22:13:20 UTC → 北京时间 2023-11-15 06:13:20
    assert convert_ts(1700000000) == "2023-11-15 06:13:20"
    assert convert_ts("1700000000000") == "2023-11-15 06:13:20"
    # 解析不了的保持原样，不要抛
    assert convert_ts("not-a-ts") == "not-a-ts"


def test_process_text_times_shifts_utc_to_beijing():
    assert convert_text_time("02:10:36-02:12:00") == "10:10:36-10:12:00"
    assert process_text_times("起止时间：02:10:36-02:12:00 患者主诉膝痛") == (
        "起止时间：10:10:36-10:12:00 患者主诉膝痛"
    )
    # 格式不符就原样返回
    assert process_text_times("起止时间：上午") == "起止时间：上午"


def test_normalize_records_accepts_dict_json_and_broken_items():
    raw = json.dumps(
        {
            "code": 200,
            "data": [
                {"timeStamp": "1700000000", "part": "起止时间：02:10:36-02:12:00 患者：膝盖疼"},
                json.dumps({"part": "医生：有发热吗"}, ensure_ascii=False),
                "不是 JSON",
            ],
        },
        ensure_ascii=False,
    )
    records = normalize_records(raw)

    assert records[0]["timeStamp"] == "2023-11-15 06:13:20"
    assert "10:10:36-10:12:00" in records[0]["part"]
    assert records[1]["part"] == "医生：有发热吗"
    assert records[2]["part"] == "不是 JSON"


def test_normalize_records_tolerates_garbage():
    assert normalize_records("用户不在线") == []
    assert normalize_records({"code": 200, "message": "用户不在线", "data": []}) == []


def test_is_semantic_valid_filters_pleasantries_but_keeps_symptoms():
    assert is_semantic_valid("嗯嗯") is False
    assert is_semantic_valid("好的") is False
    assert is_semantic_valid("谢谢") is False
    assert is_semantic_valid("。。。") is False
    assert is_semantic_valid("") is False
    assert is_semantic_valid("我膝盖疼了两周") is True
    # 敷衍词里带症状关键词时仍算有效
    assert is_semantic_valid("嗯嗯，头晕") is True
    # 原逻辑只整句匹配敷衍词：「好的，谢谢」不算整句，仍判有效
    assert is_semantic_valid("好的，谢谢") is True


def test_remove_trailing_meaningless_strips_tail():
    assert remove_trailing_meaningless("我头很痛啊") == "我头很痛"
    assert remove_trailing_meaningless("好的谢谢") == "好的"


def test_integrate_concatenates_and_cleans():
    merged = integrate("我膝盖疼", "患者：最近走路多。医生：有发热吗？")

    assert merged["arg1_text"] == "我膝盖疼"
    assert merged["result"].startswith("患者：最近走路多")
    assert "我膝盖疼" in merged["result"]
    assert merged["exceed_six"] == 0


def test_integrate_drops_meaningless_input_and_flags_long_dialogue():
    history = "。".join(f"第{index}句症状描述" for index in range(1, 8))
    merged = integrate("嗯嗯", history)

    assert merged["result"] == history
    assert merged["exceed_six"] == 1


def test_parse_summary_splits_two_blocks():
    text = "【部分对话总结】\n[张医生]：建议复查。\n【全部对话总结】\n患者膝痛两周，建议复查。"
    summary = parse_summary(text)

    assert summary["part"] == "[张医生]：建议复查。\n"
    assert summary["all"] == "患者膝痛两周，建议复查。\n"
    assert json.loads(render_summary(summary)) == summary


def test_parse_summary_handles_partial_and_empty():
    assert parse_summary("【部分对话总结】只有部分")["all"] == ""
    assert parse_summary("【全部对话总结】只有全部")["part"] == ""
    assert parse_summary("完全没有标记") == {"part": "", "all": ""}


def test_strip_zero_time_range_removes_placeholder():
    from meta_agent.flows.ai_summary.parse import strip_zero_time_range

    text = "[患者]：膝盖疼两周。\n（起止时间：00:00:00-00:00:00）\n"
    assert "00:00:00" not in strip_zero_time_range(text)
    assert "膝盖疼两周" in strip_zero_time_range(text)
    # 真实时间区间不能被删
    real = "[患者]：膝盖疼。\n（起止时间：10:10:36-10:12:00）\n"
    assert strip_zero_time_range(real) == real


# Dify「AI纪要总结」的真实输入：数组，元素是记录对象的 JSON 字符串。
RAW_RECORDS = [
    {
        "role": "Doctor",
        "timeStamp": 1794804254821,
        "from": "潘x",
        "message": "我膝盖疼了两周，走路多了更疼。。",
    },
    {
        "role": "Doctor",
        "timeStamp": 1794804267261,
        "from": "潘x",
        "message": "先做个膝关节功能评估，暂时别急着用药。。",
    },
]
RAW_RECORDS_INPUT = json.dumps(
    [json.dumps(item, ensure_ascii=False) for item in RAW_RECORDS], ensure_ascii=False
)


def test_normalize_records_accepts_dify_record_array():
    from meta_agent.flows.ai_summary.textproc import normalize_records

    records = normalize_records(RAW_RECORDS_INPUT)
    assert len(records) == 2
    assert records[0]["from"] == "潘x"
    # 13 位毫秒时间戳按北京时间换算
    assert records[0]["timeStamp"] == "2026-11-16 12:44:14"
    assert records[1]["timeStamp"] == "2026-11-16 12:44:27"


def test_render_transcript_marks_speaker_role_and_time():
    from meta_agent.flows.ai_summary.textproc import normalize_records, render_transcript

    text = render_transcript(normalize_records(RAW_RECORDS_INPUT))
    assert "[2026-11-16 12:44:14] 潘x（医生）：我膝盖疼了两周" in text
    assert text.count("\n") == 1


def test_record_span_uses_real_message_time():
    from meta_agent.flows.ai_summary.textproc import normalize_records, record_span

    assert record_span(normalize_records(RAW_RECORDS_INPUT)) == "12:44:14-12:44:27"
    # 没有时间戳时不编造区间
    assert record_span([{"message": "膝盖疼"}]) == ""


def test_apply_time_range_replaces_model_placeholder():
    from meta_agent.flows.ai_summary.parse import apply_time_range

    part = "[潘x]：患者主诉膝关节疼痛。（起止时间：00:00:00-00:00:12）\n[患者]：膝关节疼痛两周。\n"
    fixed = apply_time_range(part, "12:44:14-12:44:27")
    assert "00:00:00" not in fixed
    assert fixed.count("（起止时间：12:44:14-12:44:27）") == 2
    # 没有区间且不是 [发言人] 开头的行不硬塞
    assert apply_time_range("本段无有效医疗对话内容。\n", "12:44:14-12:44:27") == (
        "本段无有效医疗对话内容。\n"
    )
