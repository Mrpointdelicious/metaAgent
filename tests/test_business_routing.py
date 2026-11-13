"""
创建日期：2026-11-11
文件功能：验证业务路由分类器（knowledge/navigation/scene/history/patient 标记）。

回归背景：「带我去四楼」能触发场景导航，「带我回四楼」「带我到四楼」却
识别不出——_COMMAND 正则只收录了「带我去」，没有「带回/带我到」等变体。
"""

from meta_agent.domains.business_routing import classify_business_query


def test_explicit_scene_motion():
    for query in [
        "去四楼",
        "前往四楼",
        "带我去四楼",
        "带我回四楼",
        "带我到四楼",
        "带我去五楼并介绍六楼",
        "回到三楼",
        "请带我回四楼",
        "给我去一楼大厅",
    ]:
        assert classify_business_query(query).scene is True, query


def test_question_not_scene():
    for query in ["四楼有什么", "介绍五楼", "怎么去四楼", "评估室在哪"]:
        route = classify_business_query(query)
        assert route.scene is False, query


def test_history_beats_scene():
    # 「打开对话记录」虽是打开类动词，但按设计优先识别为历史对话意图。
    route = classify_business_query("帮我打开对话记录")
    assert route.history is True
    assert route.scene is False


def test_multi_intent_flags():
    route = classify_business_query("带我去五楼并介绍六楼")
    assert route.scene is True
    assert route.knowledge is True
    assert route.navigation_question is True


def test_history_flag():
    route = classify_business_query("带我去四楼并打开对话记录")
    assert route.scene is True
    assert route.history is True


def test_uncertain_not_scene():
    for query in ["能不能带我去四楼", "如果想去四楼怎么办"]:
        route = classify_business_query(query)
        assert route.scene is False, query


def test_open_record_page_is_scene():
    # 回归背景：「打开训练报告」「打开就诊记录」按提示词口径是打开页面的界面指令，
    # 但被 _PATIENT 词（训练报告）误判成患者查询，navigate_scene 被禁用。
    for query in ["打开训练报告", "帮我打开训练报告", "打开就诊记录", "请打开病历信息"]:
        route = classify_business_query(query)
        assert route.scene is True, query
        assert route.patient is False, query


def test_time_record_query_is_patient():
    # 带时间的记录请求（Dify 指令分流 code 1 的 "2"）是患者数据查询，
    # 即使动词是「打开」也查数据，不进场景跳转。
    for query in ["打开今天的训练记录", "查看今天的训练记录", "看昨天的训练报告", "修改病历"]:
        route = classify_business_query(query)
        assert route.patient is True, query
        assert route.scene is False, query
        assert route.record_query is True, query


def test_scale_and_training_intent_is_scene():
    # 量表/训练意图（"0"/"1"）走场景指令：量表→评估室映射由 scene_local 完成。
    for query in ["我想做ADL量表", "做量表", "做AD8量表", "我想做训练", "做康复训练"]:
        assert classify_business_query(query).scene is True, query


def test_scale_question_not_scene():
    # 问量表是什么 → 知识问答，不能因量表关键词进场景跳转。
    for query in ["ADL量表是什么", "MMSE怎么用", "什么是认知训练"]:
        route = classify_business_query(query)
        assert route.scene is False, query
        assert route.knowledge is True, query


def test_partial_report_name_is_patient():
    """「下肢报告」「上肢报告」这类按部位叫的报告是查患者数据，不是界面指令。"""
    for query in ["查下肢报告", "查看下肢报告", "查上肢报告", "我的康复报告在哪"]:
        route = classify_business_query(query)
        assert route.patient is True, query
        assert route.scene is False, query


def test_partial_report_name_with_open_verb_is_scene():
    """「打开下肢报告」仍是打开页面的界面指令（提示词口径）。"""
    route = classify_business_query("打开下肢报告")
    assert route.scene is True
    assert route.patient is False


def test_patient_guidance_names_tools():
    """患者数据路由必须点名具体工具，否则模型没有落点会自由作答（实测 steps 0）。"""
    hint = classify_business_query("查下肢报告").guidance()
    assert "get_irego_patient_history" in hint
    assert "get_assessment_patient_history" in hint


def test_short_followup_is_marked():
    """「查」「好」这类承接上一轮的省略追问要有 followup 标记与提示。"""
    for query in ["查", "查吧", "看看", "好"]:
        route = classify_business_query(query)
        assert route.followup is True, query
        assert route.scene is False, query
        assert "上一轮" in route.guidance(), query
    assert classify_business_query("查下肢报告").followup is False


def test_weather_chat_not_patient():
    """闲聊含「今天」不能被当成带时间的记录查询。"""
    for query in ["今天天气怎么样", "我看今天天气不错"]:
        route = classify_business_query(query)
        assert route.patient is False, query
        assert route.record_query is False, query


def test_hospital_operations_flagged():
    """运营数据请求要单独标记：它走 query_hospital_operations，不是患者数据也不是场景。"""
    for query in [
        "查医院运营数据",
        "医院运营数据",
        "今天门诊量多少",
        "床位使用率怎么样",
        "查一下运营报表",
    ]:
        route = classify_business_query(query)
        assert route.hospital_ops is True, query
        assert route.patient is False, query
        assert route.scene is False, query


def test_hospital_operations_guidance_states_permission():
    """运营数据提示必须点名工具并说明权限边界，否则模型会编造数字或指路看大屏。"""
    hint = classify_business_query("查医院运营数据").guidance()
    assert "query_hospital_directory" in hint
    assert "query_hospital_operations" in hint


def test_navigation_unaffected_by_hospital_ops():
    """「我要去五楼」仍是场景指令，不能被运营数据口径误伤。"""
    route = classify_business_query("我要去五楼")
    assert route.scene is True
    assert route.hospital_ops is False
