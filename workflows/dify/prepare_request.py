"""Dify request source. Published scene membership is validated by MetaAgent."""

import json


def main(
    query: str,
    conversation_id: str,
    user_id: str,
    app_id: str,
    workflow_run_id: str,
    project_patient_id: str,
    demo_role: str = "patient",
    demo_space_id: str = "",
    demo_scene_version: str = "0",
    space_id: str = "",
    scene_id: str = "",
) -> dict:
    if not all((conversation_id, user_id, app_id, workflow_run_id)):
        raise ValueError("会话信息不完整，请开启新对话。")
    patient = str(project_patient_id)
    if not patient.isascii() or not patient.isdigit() or int(patient) <= 0:
        raise ValueError("展示患者绑定须为 project.dbuser 的正整数 ID。")
    if demo_role not in {"patient", "clinician", "operator"}:
        raise ValueError("角色仅允许 patient、clinician、operator。")
    if not str(demo_scene_version).isascii() or not str(demo_scene_version).isdigit():
        raise ValueError("sceneVersion 必须是非负整数。")
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise ValueError("query 须为 1–4000 字符的非空问题。")
    selected_space = (space_id or demo_space_id or "").strip()
    selected_scene = (scene_id or "").strip()
    if selected_scene and not selected_space:
        raise ValueError("提供 sceneId 时必须同时提供 spaceId。")
    if len(selected_space) > 160 or len(selected_scene) > 160:
        raise ValueError("spaceId 和 sceneId 不能超过160字符。")
    payload = {
        "request_id": "dify:" + app_id + ":" + workflow_run_id,
        "conversation_id": "dify:" + app_id + ":" + conversation_id,
        "user": "dify:" + user_id,
        "inputs": {
            "tenantId": "local-dify-demo",
            "role": demo_role,
            "projectPatientId": str(int(patient)),
            "sceneVersion": int(demo_scene_version),
        },
        "query": query.strip(),
        "response_mode": "blocking",
    }
    if selected_space:
        payload["inputs"]["spaceId"] = selected_space
    if selected_scene:
        payload["inputs"]["sceneId"] = selected_scene
    if any(len(payload[key]) > 160 for key in ("request_id", "conversation_id", "user")):
        raise ValueError("请求或会话标识超过 160 字符。")
    return {"request_body": json.dumps(payload, ensure_ascii=False)}
