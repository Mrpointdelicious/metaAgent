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
