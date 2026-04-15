from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from agent import OrchestratorRequest, OrchestratorResponse
from agent.schemas import OrchestrationTaskType, normalize_task_type
from config import Settings


COMMAND_NAMES = {"review-patient", "screen-risk", "weekly-report"}
SCRIPT_NAMES = {"main.py", "cli.py"}
PYTHON_NAMES = {"python", "python.exe", "py", "py.exe"}
WEEKLY_KEYWORDS = ("周报", "weekly", "summary", "摘要")
SCREEN_KEYWORDS = ("风险筛选", "高风险", "优先复核", "risk", "screen")
REVIEW_KEYWORDS = ("复核", "计划", "患者", "病人", "review", "plan", "patient")
GAIT_KEYWORDS = ("步态", "步道", "gait", "walkway", "walk")
FOLLOW_UP_KEYWORDS = ("换成", "改成", "调整", "继续", "刚才", "这个", "same", "switch", "change", "previous")
DETAIL_KEYWORDS = ("详细", "原因", "detail", "detailed", "reason", "why")
BRIEF_KEYWORDS = ("简短", "简洁", "brief", "short")


@dataclass
class ConversationState:
    task_type: str | None = None
    patient_id: int | None = None
    plan_id: int | None = None
    therapist_id: int | None = None
    days: int | None = None
    top_k: int = 10
    response_style: str = "standard"
    need_gait_evidence: bool = False


def build_welcome(settings: Settings) -> str:
    return f"""康复计划执行偏离复核 Demo
稳定演示样本:
  therapist_id={settings.demo_default_therapist_id}
  plan_id={settings.demo_default_plan_id}
  patient_id={settings.demo_default_patient_id}

结构化命令:
  review-patient --plan-id {settings.demo_default_plan_id} --days 30
  screen-risk --therapist-id {settings.demo_default_therapist_id} --days 30
  weekly-report --therapist-id {settings.demo_default_therapist_id} --days 30

自然语言:
  帮我复核计划 {settings.demo_default_plan_id}
  看一下医生 {settings.demo_default_therapist_id} 最近 30 天的高风险患者
  给我这个医生最近 7 天的周报
  换成最近 7 天

运行时 LLM 切换:
  set-provider openai|qwen|deepseek
  set-model <model_name>
  set-base-url <url>
  set-agent on|off
  show-llm
  clear-llm
  set-trace on|off

会话相关:
  show-context
  clear-context
  show-demo-sample

其他:
  help
  exit
"""


def summarize_context(state: ConversationState, settings: Settings) -> str:
    return (
        f"任务类型={state.task_type or '未设置'} "
        f"治疗师ID={state.therapist_id or settings.demo_default_therapist_id} "
        f"患者ID={state.patient_id or '未设置'} "
        f"计划ID={state.plan_id or settings.demo_default_plan_id} "
        f"时间窗={state.days or '默认'}天 "
        f"返回数量={state.top_k} "
        f"响应风格={state.response_style} "
        f"需要步态证据={state.need_gait_evidence}"
    )


def demo_sample_text(settings: Settings) -> str:
    return (
        f"稳定 Demo 样本: therapist_id={settings.demo_default_therapist_id}, "
        f"plan_id={settings.demo_default_plan_id}, patient_id={settings.demo_default_patient_id}。"
    )


def tokenize_input(raw: str) -> list[str]:
    try:
        return shlex.split(raw, posix=False)
    except ValueError:
        return raw.split()


def normalize_cli_tokens(raw: str) -> tuple[list[str] | None, str | None]:
    tokens = tokenize_input(raw)
    if not tokens:
        return None, None

    for index, token in enumerate(tokens):
        if _basename(token) in SCRIPT_NAMES:
            tail = tokens[index + 1 :]
            if not tail:
                return [], "当前已经在 Demo 交互模式里，不要再次输入 python 启动命令，直接输入需求即可。"
            return tail, None

    if _basename(tokens[0]) in PYTHON_NAMES:
        return [], "当前已经在 Demo 交互模式里，不要再次输入 python 启动命令，直接输入需求即可。"

    if _is_cli_start(tokens[0]):
        return tokens, None
    return None, None


def parse_natural_language_request(
    raw: str,
    state: ConversationState,
    settings: Settings,
    *,
    use_agent_sdk: bool,
    llm_provider: str | None,
    llm_model: str | None,
    llm_base_url: str | None,
) -> tuple[OrchestratorRequest | None, str | None]:
    text = raw.strip()
    follow_up = _is_follow_up(text)
    task_type = _infer_task_type(text, state)
    if task_type is None:
        return None, "没有识别到任务类型。可以直接说“帮我复核计划 6”或“给我医生 56 最近 7 天周报”。"

    patient_id = _extract_identifier(text, ("患者", "病人", "patient"))
    plan_id = _extract_identifier(text, ("计划", "plan"))
    therapist_id = _extract_identifier(text, ("医生", "治疗师", "康复师", "doctor", "therapist"))
    days = _extract_days(text)
    top_k = _extract_top_k(text)
    response_style = _extract_response_style(text) or (state.response_style if follow_up else "standard")
    need_gait_evidence = _wants_gait_evidence(text)

    context = {
        "task_type": state.task_type,
        "patient_id": state.patient_id,
        "plan_id": state.plan_id,
        "therapist_id": state.therapist_id,
        "days": state.days,
        "top_k": state.top_k,
        "response_style": state.response_style,
        "need_gait_evidence": state.need_gait_evidence,
    }

    note: str | None = None
    if task_type in {OrchestrationTaskType.SCREEN_RISK.value, OrchestrationTaskType.WEEKLY_REPORT.value}:
        therapist_id = therapist_id or state.therapist_id or settings.demo_default_therapist_id
        if therapist_id == settings.demo_default_therapist_id and state.therapist_id is None and _extract_identifier(text, ("医生", "治疗师", "doctor", "therapist")) is None:
            note = f"未提供医生 ID，已使用稳定 Demo 医生 {settings.demo_default_therapist_id}。"
        days = days or (state.days if follow_up else None) or settings.default_weekly_report_days
        top_k = top_k or state.top_k or 10
        return (
            OrchestratorRequest(
                task_type=task_type,
                therapist_id=therapist_id,
                days=days,
                top_k=top_k,
                raw_text=text,
                use_agent_sdk=use_agent_sdk,
                llm_provider=llm_provider,
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                response_style=response_style,
                need_gait_evidence=need_gait_evidence,
                context=context,
            ),
            note,
        )

    if task_type == OrchestrationTaskType.GAIT_REVIEW.value:
        patient_id = patient_id or state.patient_id or settings.demo_default_patient_id
        days = days or (state.days if follow_up else None) or settings.default_time_window_days
        if patient_id == settings.demo_default_patient_id and state.patient_id is None and _extract_identifier(text, ("患者", "病人", "patient")) is None:
            note = f"未提供患者 ID，已使用稳定 Demo 患者 {settings.demo_default_patient_id}。"
        return (
            OrchestratorRequest(
                task_type=task_type,
                patient_id=patient_id,
                days=days,
                raw_text=text,
                use_agent_sdk=use_agent_sdk,
                llm_provider=llm_provider,
                llm_model=llm_model,
                llm_base_url=llm_base_url,
                need_gait_evidence=True,
                response_style=response_style,
                context=context,
            ),
            note,
        )

    plan_id = plan_id or (state.plan_id if follow_up else None)
    patient_id = patient_id or (state.patient_id if follow_up else None)
    therapist_id = therapist_id or (state.therapist_id if follow_up else None)
    days = days or (state.days if follow_up else None) or settings.default_time_window_days
    if plan_id is None and patient_id is None:
        return None, "单患者复核需要计划 ID 或患者 ID，例如：帮我复核计划 6。"
    return (
        OrchestratorRequest(
            task_type=OrchestrationTaskType.REVIEW_PATIENT.value,
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
            raw_text=text,
            use_agent_sdk=use_agent_sdk,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            need_gait_evidence=need_gait_evidence,
            response_style=response_style,
            context=context,
        ),
        note,
    )


def update_state_from_response(
    state: ConversationState,
    request: OrchestratorRequest,
    response: OrchestratorResponse,
) -> None:
    normalized_task = normalize_task_type(response.task_type).value
    if normalized_task == OrchestrationTaskType.UNKNOWN.value:
        return
    state.task_type = normalized_task
    state.days = request.days or state.days
    state.top_k = request.top_k or state.top_k
    state.response_style = request.response_style or state.response_style
    state.need_gait_evidence = bool(request.need_gait_evidence)
    if request.therapist_id is not None:
        state.therapist_id = request.therapist_id
    if request.plan_id is not None:
        state.plan_id = request.plan_id
    if request.patient_id is not None:
        state.patient_id = request.patient_id

    payload = response.structured_output
    if normalized_task == OrchestrationTaskType.REVIEW_PATIENT.value and isinstance(payload, dict):
        state.patient_id = payload.get("patient_id") or state.patient_id
        state.plan_id = payload.get("primary_plan_id") or state.plan_id
        state.therapist_id = payload.get("therapist_id") or state.therapist_id
    if normalized_task in {OrchestrationTaskType.SCREEN_RISK.value, OrchestrationTaskType.WEEKLY_REPORT.value} and isinstance(payload, dict):
        state.therapist_id = payload.get("therapist_id") or state.therapist_id
    if normalized_task == OrchestrationTaskType.GAIT_REVIEW.value and isinstance(payload, dict):
        state.patient_id = payload.get("patient_id") or state.patient_id


def _basename(token: str) -> str:
    return token.strip().strip('"').strip("'").replace("\\", "/").rstrip("/").split("/")[-1].lower()


def _is_cli_start(token: str) -> bool:
    return _basename(token) in COMMAND_NAMES or token.startswith("--")


def _is_follow_up(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in FOLLOW_UP_KEYWORDS)


def _infer_task_type(text: str, state: ConversationState) -> str | None:
    lowered = text.lower()
    if any(keyword in text or keyword in lowered for keyword in WEEKLY_KEYWORDS):
        return OrchestrationTaskType.WEEKLY_REPORT.value
    if any(keyword in text or keyword in lowered for keyword in SCREEN_KEYWORDS):
        return OrchestrationTaskType.SCREEN_RISK.value
    if any(keyword in text or keyword in lowered for keyword in GAIT_KEYWORDS) and _extract_identifier(text, ("患者", "病人", "patient")) is not None:
        return OrchestrationTaskType.GAIT_REVIEW.value
    if _extract_identifier(text, ("计划", "plan")) is not None or _extract_identifier(text, ("患者", "病人", "patient")) is not None:
        return OrchestrationTaskType.REVIEW_PATIENT.value
    if any(keyword in text or keyword in lowered for keyword in REVIEW_KEYWORDS):
        return OrchestrationTaskType.REVIEW_PATIENT.value
    if _extract_identifier(text, ("医生", "治疗师", "康复师", "doctor", "therapist")) is not None:
        if state.task_type in {OrchestrationTaskType.WEEKLY_REPORT.value, OrchestrationTaskType.SCREEN_RISK.value}:
            return state.task_type
        return OrchestrationTaskType.SCREEN_RISK.value
    if _is_follow_up(text):
        return state.task_type
    return None


def _extract_identifier(text: str, labels: tuple[str, ...]) -> int | None:
    for label in labels:
        pattern = rf"{re.escape(label)}\s*(?:id)?\s*[:：]?\s*(\d+)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_days(text: str) -> int | None:
    lowered = text.lower()
    if "本周" in text or "最近一周" in text or "近一周" in text or "last week" in lowered:
        return 7
    if "本月" in text or "最近一个月" in text or "近一个月" in text or "last month" in lowered:
        return 30
    for pattern in (
        r"(?:最近|过去|近)\s*(\d+)\s*天",
        r"last\s*(\d+)\s*days?",
        r"(\d+)\s*天",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_top_k(text: str) -> int | None:
    for pattern in (r"top\s*(\d+)", r"前\s*(\d+)", r"(\d+)\s*个"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_response_style(text: str) -> str | None:
    lowered = text.lower()
    if any(keyword in text or keyword in lowered for keyword in DETAIL_KEYWORDS):
        return "detailed"
    if any(keyword in text or keyword in lowered for keyword in BRIEF_KEYWORDS):
        return "brief"
    return None


def _wants_gait_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in GAIT_KEYWORDS)
