"""Dify Code node: present answers first, operational messages at the end."""

import json
import re
from urllib.parse import urlparse

LABELS = {
    "get_patient_profile": "查询基本信息",
    "get_patient_consultation": "查询就诊记录",
    "get_patient_rehab": "查询康复计划与训练记录",
    "get_iretour_training_history": "查询 IReTour 训练历史",
    "get_iretour_patient_context": "查询 IReTour 数据覆盖范围",
    "get_iretour_patient_history": "查询 IReTour 训练历史",
    "get_iretour_session_analysis": "分析 IReTour 单次训练",
    "get_iretour_longitudinal_analysis": "分析 IReTour 训练趋势",
    "generate_iretour_single_session_report": "生成 IReTour 单次报告",
    "generate_iretour_longitudinal_report": "生成 IReTour 趋势报告",
    "get_irego_training_history": "查询 IReGo 训练历史",
    "get_irego_patient_history": "查询 IReGo 训练历史",
    "get_irego_session_analysis": "分析 IReGo 单次训练",
    "get_irego_longitudinal_analysis": "分析 IReGo 训练趋势",
    "generate_irego_single_session_report": "生成 IReGo 单次报告",
    "generate_irego_longitudinal_report": "生成 IReGo 趋势报告",
    "search_knowledge": "检索康复知识",
    "search_doctors": "查询就诊资源",
    "query_hospital_operations": "查询医院运营信息",
    "perform_scene_action": "处理场景请求",
    "navigate_scene": "处理场景请求",
}
STATUS = {
    "succeeded": "完成", "partial": "部分完成", "unavailable": "暂不可用",
    "failed": "未完成", "unsupported": "当前不支持", "cancelled": "已取消",
    "expired": "已过期", "running": "处理中", "pending": "等待处理",
}


def main(body: str, status_code: int) -> dict:
    fallback = "本次未能取得回答，请稍后重试。"
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict) or int(status_code or 0) not in (200, 202):
        return {"display_text": fallback + "\n\n---\n**本次查询过程**\n\n- 服务暂不可用。",
                "run_status": "unavailable", "run_id": "", "conversation_id": ""}

    events = data.get("events") or []
    latest = {}
    notices = []
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        text = payload.get("text") or payload.get("message") or ""
        if event.get("type") == "answer_part" and text:
            latest[event.get("goal_id") or "agent"] = text
        elif event.get("type") in ("clarification", "task_failed") and text:
            notices.append(text)
    answer = "\n\n".join(latest.values()).strip()
    if not answer:
        answer = "\n\n".join(dict.fromkeys(notices)).strip() or fallback
    # Render verified retrieval provenance separately from model-authored claims.
    source_lines = []
    seen_sources = set()
    for retrieval in (data.get("metrics") or {}).get("knowledge_retrieval", []):
        for source in retrieval.get("sources", []):
            key = (retrieval.get("source_id"), source.get("ref") or source.get("version_id"),
                   source.get("page"))
            if key in seen_sources:
                continue
            seen_sources.add(key)
            title = str(source.get("title") or "文献").replace("\n", " ")
            title = title.replace("<", "＜").replace(">", "＞")
            title = title.replace("[", "［").replace("]", "］")
            page = source.get("page")
            section = str(source.get("locator") or source.get("section") or "原文分段")
            section = section.replace("<", "＜").replace(">", "＞")
            section = section.replace("[", "［").replace("]", "］")
            location = "PDF 第 " + str(page) + " 页" if page else section
            locator = str(source.get("source_locator") or "")
            parsed_source = urlparse(locator)
            safe_source = (
                parsed_source.scheme == "https"
                and parsed_source.hostname in (
                    "doi.org", "www.nice.org.uk", "www.who.int", "iris.who.int"
                )
                and not parsed_source.username and not parsed_source.password
            )
            label = title
            if safe_source:
                link = locator.replace("(", "%28").replace(")", "%29")
                label = "[" + title + "](" + link + ")"
            line = "- " + label + "（" + location + "）"
            source_lines.append(line)
    if source_lines:
        answer += "\n\n**本次检索资料**\n\n" + "\n".join(source_lines[:12])

    status = str(data.get("status") or "unavailable")
    if status == "running":
        answer = "本次请求仍在处理中，请稍后查看或重试。"

    # A text link does not display the image. Render each verified artifact once.
    for event in events:
        if event.get("type") != "artifact_ready":
            continue
        url = str((event.get("payload") or {}).get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.hostname in (
            "127.0.0.1", "localhost", "host.docker.internal"
        ) and not parsed.username and not parsed.password:
            image_pattern = r"!\[[^\]\n]*\]\(\s*<?" + re.escape(url) + r">?(?:\s+[^)\n]*)?\s*\)"
            if not re.search(image_pattern, answer):
                answer += "\n\n![训练报告](" + url + ")"

    names = [tool.get("name", "")
             for step in (data.get("metrics") or {}).get("agent_model_steps", [])
             for tool in step.get("tools", [])]
    tasks = data.get("task_statuses") or {}
    lines = []
    if len(names) == len(tasks):
        for index, name in enumerate(names, 1):
            result = tasks.get("agent_tool_" + str(index), "")
            label = LABELS.get(name, "查询相关信息")
            lines.append(label + ("：" + STATUS.get(result, "未完成") if result else ""))
    elif tasks:
        lines.append("本轮执行了 " + str(len(tasks)) + " 项查询。")
    else:
        lines.append("使用本轮输入或已有会话信息组织回答。")
    # Preserve user-facing progress messages while consolidating repetitive polling text.
    for event in events:
        if event.get("type") == "progress":
            text = (event.get("payload") or {}).get("text", "")
            if text and text != "正在查询相关信息。":
                lines.append(text)
    lines.extend(notices)
    lines = list(dict.fromkeys(lines))
    lines.append("本次处理：" + STATUS.get(status, "未完成") + "。")
    display = answer + "\n\n---\n**本次查询过程**\n\n" + "\n".join("- " + text for text in lines)
    return {"display_text": display, "run_status": status,
            "run_id": str(data.get("run_id") or ""),
            "conversation_id": str(data.get("conversation_id") or "")}
