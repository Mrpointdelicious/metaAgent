"""
创建日期：2026-11-18
文件功能：AI问诊独立流程 —— 对齐 Dify「AI诊室(测试)」真实链路（advanced-chat，78 节点）。

真实链路（已按当前服务内网化，替换公网 IP）：

  1. webyun 登录取 token（ga-robot.com/api/Login/Login，服务账号在 settings）；
  2. 意图识别（LLM 语义分类为主 + 规则保底）：
     场景跳转白名单硬前置（点位来自本地目录，不让 LLM 编造）→ LLM 语义分类
     → 失败降级规则引擎（intents.py：AI问诊检测 > 健康咨询快配 > 医疗动作 > 非问诊 > qa）；
  3. 按意图分流：
     - 场景跳转（selection_mode=scene）→ 复用现有服务场景管线：原话交给
       navigate_scene（scene_local 本地解析 → 原话覆盖/目录协议校验 →
       action_ready 交付事件），命令编码由管线独立下发，不走白名单直拼；
     - ai_consultation → AI 分诊（diagnosis.py：病史 → LLM 6/7 → 初步判断
       → 疾病关键词匹配 → 自动挂号拼接）；
     - visit_or_medical_request / medical_action_request → 挂号（booking.py：
       在线医生 0/1/多 三态 + [DoctorRecommend:]）；
     - qa / 其他 → 知识问答（question_classify + error_code + MaxKB 检索）：
       CLASS_2 错误码走严格作答提示词，CLASS_3 人员/医生咨询转挂号分支，
       其余走产品问答提示词；知识源为 MaxKB（knowledge.py），
       替代 Dify 原版的内置数据集与 richtxt 企业知识。

与 Dify 的差异（有意为之）：
  - 场景指令复用本服务 navigate_scene 管线（本地解析器 + action_ready 交付），
    不用 Dify 原版的静态白名单直拼命令串；
  - 企业知识（richtxt richId）并入 MaxKB 检索；
  - 各 HTTP 源失败均降级继续（病史缺失按无病史分诊、医生名单不可用给
    降级话术、知识检索失败按无命中自答），仅 LLM 无输出视为不可用。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meta_agent.adapters.text import model_text
from meta_agent.agent.tools import invoke_tool
from meta_agent.contracts import DomainError
from meta_agent.domains.business_routing import classify_business_query
from meta_agent.flows.fixed_task import FixedTaskFlow

from .booking import CLASS3_LABEL, run_booking
from .diagnosis import run_triage
from .error_code import extract_error_code
from .intents import classify_intent, detect_scene_jump, emit
from .knowledge import search_maxkb
from .llm_intent import classify_intent_llm
from .prompts import build_error_prompt, build_product_prompt
from .question_classify import classify_question

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

_SCENE_NO_MATCH = "[Answer:]抱歉，没有找到要去的场景点位，请换种说法试试。"

# 粘滞意图：这些状态代表「正在问诊」，`qa` 兜底不应把它冲掉。规则识别难免
# 误判，一旦某轮误落 qa 就把状态写成 qa，后面省略句（「第二个」）拿到的历史
# 全是 qa，续聊判定失效，整条问诊链就退化成知识库问答。
_STICKY_INTENTS = frozenset({"ai_consultation"})


def _next_intent_state(previous: str, current: str, selection_mode: str) -> str:
    """计算写回 memory.variables 的问诊意图状态。

    - 场景跳转：用户已被带离问诊，显式清空；
    - 问诊 / 医疗动作：明确意图，照常写入；
    - qa 兜底：若上一轮仍在问诊则保持，避免一次误判永久断链；
    - 其他明确新意图（天气/时间等）：照常写入，结束问诊。
    """
    if selection_mode == "scene":
        return ""
    if current in ("ai_consultation", "visit_or_medical_request", "medical_action_request"):
        return current
    if current == "qa" and previous in _STICKY_INTENTS:
        return previous
    return current


async def scene_delivery(ctx: RunContext) -> str | None:
    """场景动作：复用现有服务的 navigate_scene 完整管线。

    把本轮原话交给 invoke_tool("navigate_scene")（与 default 分支的工具口径
    一致）：scene_local 本地解析（口语归一/同义词/拼音/多动作拆句）→ 原话
    覆盖与目录协议校验 → dispatch 交付。命令编码（[ScenePoint:]21 等）由管线
    经 action_ready 事件独立下发——流式模式是独立消息帧，非流式模式拼在
    answer 前面（api/routes.py），本函数返回值只承担给用户的确定性话术。

    返回 None 表示未命中或场景服务不可用，调用方可继续别的分支；
    命中（含歧义澄清）返回给用户的话术。
    """
    if not (ctx.settings.scene_enabled and ctx.settings.scene_actions_enabled):
        return None
    try:
        await invoke_tool(ctx, "navigate_scene", {"target": ctx.query})
    except DomainError as exc:
        logger.info(
            "ai_consultation scene pipeline skipped",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_scene_skipped",
                "fields": {"code": exc.code},
            },
        )
        if exc.outcome == "clarification":
            return "[Answer:]" + exc.message
        return None
    if not ctx.record.action_delivery:
        return None
    names = [
        action.get("matchName")
        for result in ctx.record.results.values()
        for action in (result.outputs.get("actions") or {}).values()
        if action.get("status") == "matched" and action.get("matchName")
    ]
    # 必须带 [Answer:] 前缀：非流式响应 routes.py 会把 action_ready 的
    # command_code（如 [ScenePoint:]5）直接拼到 answer 前面，缺前缀就会变成
    # "[ScenePoint:]5好的，这就带您去二楼。"，前端无法把指令与话术分段。
    return (
        f"[Answer:]好的，这就带您去{names[0]}。"
        if names
        else "[Answer:]好的，已经为您带路。"
    )


async def run_scene(ctx: RunContext, intent: dict[str, Any]) -> str:
    """场景跳转分支：意图白名单命中后交现有管线执行，按交付状态回复。

    白名单的 selection_value（楼层/点位关键词）仅用于日志留痕；匹配与交付
    全部交给 navigate_scene 管线，不再自行拼 [ScenePoint:] 命令串。
    """
    logger.info(
        "ai_consultation scene branch",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "consultation_scene_branch",
            "fields": {
                "target": str(intent.get("selection_value") or ""),
                "keyword": str(intent.get("matched_keyword") or ""),
            },
        },
    )
    return await scene_delivery(ctx) or _SCENE_NO_MATCH


async def run_knowledge(ctx: RunContext, model: Any) -> str:
    """知识问答分支：CLASS_3 转挂号；错误码走严格作答；其余走产品问答（MaxKB）。"""
    if model is None:
        raise DomainError(
            "consultation_model_unavailable", "问答模型暂不可用。", outcome="unavailable"
        )

    query = ctx.query
    # 对齐 Dify qa 分支的次序：动作指令先试场景管线（如「帮我打开减重系统」），
    # 未命中才进入知识问答（条件分支 16 的 null 出口）。用轻量规则先筛，
    # 纯知识问题（「irego怎么开机」/错误码）不再白跑一次 navigate_scene 与审计。
    if classify_business_query(query).scene:
        scene_reply = await scene_delivery(ctx)
        if scene_reply is not None:
            return scene_reply

    label_info = classify_question(query)
    label = str(label_info.get("label") or "")
    if label == CLASS3_LABEL:
        return await run_booking(ctx, query, named=True)

    result = await search_maxkb(ctx, query)
    kb_context = str((result or {}).get("context") or "")
    use_knowledge = bool(kb_context.strip()) and str((result or {}).get("status")) != "no_evidence"

    error_code = extract_error_code(query)
    if label == "设备错误码提示" or error_code:
        prompt = build_error_prompt(error_code or query, kb_context if use_knowledge else "")
    else:
        prompt = build_product_prompt(query, kb_context, use_knowledge)

    await ctx.llm_budget.take()
    response = await model.ainvoke(
        prompt,
        config={
            "tags": ["ai_consultation", "knowledge"],
            "metadata": {"component": "ai_consultation_knowledge"},
        },
    )
    text = model_text(response.content if hasattr(response, "content") else str(response)).strip()
    if not text:
        raise DomainError("consultation_empty", "暂未生成有效回复。", outcome="unavailable")
    return "[Answer:]" + text


class Flow(FixedTaskFlow):
    branch = "ai_consultation"
    task_name = "AI问诊"
    metrics_key = "consultation_answered"

    async def _pipeline(self, ctx: RunContext, model: Any) -> str:
        # 上一轮的意图决定本轮省略句（如「还失眠」）是否续走分诊。移植的
        # 意图引擎只认 [{"intent": ...}] 形态，存 memory.variables 跨轮持久化；
        # 不复用 memory.turns（那是 user/assistant 文本，引擎读不到 intent）。
        variables = getattr(ctx.memory, "variables", None)
        if variables is None:
            variables = {}
            ctx.memory.variables = variables
        previous_intent = str(variables.get("consultation_intent") or "")
        conversation_history = [{"intent": previous_intent}] if previous_intent else []

        # 场景跳转白名单硬前置：点位 ID 必须来自本地目录，不能由 LLM 编造。
        scene_result = detect_scene_jump(ctx.query)
        if scene_result["matched"]:
            intent_source = "scene_whitelist"
            intent = emit(
                "visit_or_medical_request",
                f"场景跳转: {scene_result['matched_keyword']} -> {scene_result['target']}",
                0.95,
                sel_target="jump",
                sel_mode="scene",
                sel_value=scene_result["target"],
            )
        else:
            intent = None
            intent_source = "rules"
            # LLM 语义分类（默认开启）；模型不可用/预算耗尽/输出非法时自动降级规则。
            if getattr(ctx.settings, "consultation_intent_llm", False) and model is not None:
                intent = await classify_intent_llm(ctx, model, ctx.query, previous_intent)
                if intent is not None:
                    intent_source = "llm"
            if intent is None:
                intent = classify_intent(ctx.query, conversation_history=conversation_history)

        name = str(intent.get("intent") or "qa")
        ctx.record.metrics["consultation_intent"] = name
        ctx.record.metrics["consultation_intent_source"] = intent_source
        ctx.record.metrics["consultation_intent_confidence"] = str(
            intent.get("confidence") or ""
        )
        # 本轮 qa 不冲掉活跃问诊态；场景跳转则清空。下一轮据此判定省略句续聊。
        next_state = _next_intent_state(
            previous_intent, name, str(intent.get("selection_mode") or "")
        )
        ctx.record.metrics["consultation_intent_state"] = next_state or "none"
        variables["consultation_intent"] = next_state

        if intent.get("selection_mode") == "scene":
            return await run_scene(ctx, intent)
        if name == "ai_consultation":
            return await run_triage(ctx, model)
        if name in ("visit_or_medical_request", "medical_action_request"):
            return await run_booking(ctx, ctx.query, named=False)
        return await run_knowledge(ctx, model)
