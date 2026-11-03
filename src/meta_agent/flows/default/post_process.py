"""
创建日期：2026-10-10
文件功能：默认分支的后处理——把 agent 答复 + 工具结果编译成给前端的协议。

TODO（你在这里接 Dify 里 main() 的逻辑）：
    post_process(ctx, answer, collected) -> dict
      - answer    ~ 原 main() 的 arg1（agent 答复文本）
      - collected ~ 原 main() 的 agent_trace（工具调用 + 原始契约），
                    由 meta_agent.agent.results.collect_agent_results 产出
    返回 dict 的 answer 字段（如 "[Answer:]..."）会被作为 answer_part.text
    下发给前端；dict 里其余字段（command/Cweb/web/...）当前不随事件下发。

    字段对照你 main() 的返回：
      answer / command / Cweb / web / query_context / evidence_count
      / command_1..command_5 / timestamp

    移植注意（已确认）：
      - 工具名映射：KINDS 里的 504 工具名要换成 MetaAgent 工具名；
      - 契约格式：collected[].raw 直接是新后端契约，无需再拆 504-compact-v1 信封。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meta_agent.domains.chat_history import build as build_chat_history_url
from meta_agent.domains.hospital import hospital_report_urls, render_hospital_markdown
from meta_agent.tools.ai_webapi import normalize_artifact_url

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext


# 生成报表的工具：命中后把报告图片链接前置为 [Cweb]，并用品读短句覆盖 LLM 长答复。
REPORT_TOOLS = {
    "generate_irego_single_session_report",
    "generate_irego_longitudinal_report",
    "generate_iretour_single_session_report",
    "generate_iretour_longitudinal_report",
}


def _report_message(tool: str) -> str:
    if "longitudinal" in tool:
        return "连续训练趋势报告已生成，请查看下方图片。"
    return "单次训练报告已生成，请查看下方图片。"


def _public_artifact_url(ctx: RunContext, url: str) -> str:
    """把后端返回的制品 URL 重写到公开反代前缀；未配置前缀时原样返回。"""
    settings = getattr(ctx, "settings", None)
    if settings is None:
        return url
    return normalize_artifact_url(settings, url)


def _web_page_url(ctx: RunContext) -> str:
    """生成报表时与 [Cweb] 成对输出的 [web] 页面地址。

    有 [Cweb] 就必须有 [web]；地址与「打开历史对话记录」完全一致（复用同一个
    chat_history.build），保证两个入口拼出的 user/space 链接不漂移。
    """
    out = build_chat_history_url(
        str(getattr(ctx.scope, "end_user_id", "") or ""),
        str(getattr(ctx.scope, "space_id", "") or ""),
    )
    return out.get("legacy_web") or ""


def post_process(
    ctx: RunContext,
    answer: str | None,
    collected: list[dict[str, Any]],
    include_timestamp: bool = True,
) -> dict[str, Any]:
    # TODO: 替换成你 Dify 里 main() 的完整逻辑（下面先把指令命令前置到答复前）。
    # 场景/医生指令段（[ScenePoint:]/[DoctorRecommend:]）已在工具返回时作为独立 message
    # 实时下发（见 agent.tools.invoke_tool），此处不再拼进最终答复。
    command = ""
    if not command:
        # 打开历史对话记录：legacy_web 即 "[web]<url>"，前置到答复前；
        # 答复文本用工具的 patient_message（不采纳 LLM 可能拼出的网址/多余话术）。
        for item in collected:
            raw = item.get("raw") or {}
            if item.get("tool") == "open_chat_history":
                command = raw.get("legacy_web") or ""
                if command:
                    answer = raw.get("patient_message") or answer
                break
    # 生成报表：报告图片链接前置为 [Cweb]（工具返回啥就输出啥，不改写）；
    # 有 [Cweb] 就必须有 [web] 页面地址（拼 user/space 生成），二者成对输出；
    # 答复用品读短句覆盖 LLM 长答复，避免把 markdown 图片或多余话术塞进最终答复。
    cweb = ""
    web = ""
    for item in collected:
        raw = item.get("raw") or {}
        if item.get("tool") not in REPORT_TOOLS:
            continue
        data = raw.get("data") or {}
        image_url = str(data.get("image_url") or "").strip()
        if not image_url:
            continue
        cweb = "[Cweb]" + _public_artifact_url(ctx, image_url)
        web = _web_page_url(ctx)
        answer = _report_message(item.get("tool"))
        break
    # 医院运营：用确定性表格渲染覆盖 LLM 自由答复（对齐 Dify render 代码节点）；
    # 报表图片前置为 [Cweb]、完整报告页为 [web]（有 [Cweb] 必须有 [web]），正文仍是表格。
    for item in collected:
        if item.get("tool") != "query_hospital_operations":
            continue
        raw = item.get("raw") or {}
        rendered = render_hospital_markdown(raw)
        if rendered:
            answer = rendered
        report = hospital_report_urls(raw)
        if report:
            cweb = "[Cweb]" + _public_artifact_url(ctx, report)
            web = _web_page_url(ctx)
        break
    # 时间戳作为独立 message 分段下发：前置 [timestamp=…] 进 answer，由 DifyEventAdapter 的
    # split_answer_segments 拆成单独 message（如 "[timestamp=150.26]" + "[Answer:]…"）。
    # 问诊/放房间分支由调用方传 include_timestamp=False 关闭。
    timestamp = ""
    if include_timestamp:
        raw_ts = (ctx.record.raw_inputs or {}).get("timestamp")
        timestamp = str(raw_ts).strip() if raw_ts not in (None, "") else ""
    timestamp_prefix = "[timestamp=" + timestamp + "]" if timestamp else ""
    result = {
        # 最终文本 = [timestamp]时间戳 + 指令命令 + [Cweb]报告图 + [web]页面 + [Answer:]答复，
        # 如 "[timestamp=150.26][Answer:]查到了…"。
        "answer": timestamp_prefix + command + cweb + web + "[Answer:]" + (answer or ""),
        "command": command,
        "timestamp": timestamp,
        "Cweb": cweb,
        "web": web,
        "query_context": "",
        "evidence_count": str(len(collected)),
    }
    # 把最终回复完整写进日志，便于按 run_id 排查「输入 → 输出」。
    logger.info(
        "final answer",
        extra={
            "run_id": getattr(ctx.record, "run_id", ""),
            "event_type": "answer",
            "fields": {"answer": result["answer"]},
        },
    )
    return result
