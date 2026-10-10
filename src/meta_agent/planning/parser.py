"""
创建日期：2026-09-08
文件功能：从原话和有界会话引用理解多意图，模型失败时仅使用明确模式兜底。
"""

import asyncio
import re
from typing import Any, Protocol

from pydantic import ValidationError

from meta_agent.config import Settings
from meta_agent.context.budget import LLMBudget, estimate_tokens
from meta_agent.contracts import (
    ConversationState,
    DomainError,
    Goal,
    HospitalRequest,
    IntentDecision,
    IReGoRequest,
    IReTourRequest,
    Selector,
)
from meta_agent.prompts.contracts import (
    EmptyPromptInputs,
    IntentPromptInputs,
    PromptBinding,
)
from meta_agent.prompts.service import PromptService

ACTION = r"(?:打开|关闭|关掉|开启|点开|前往|进入|退出|返回|回到|去|显示|隐藏|确认|取消|挥手)"
NEGATIVE = re.compile(r"不要|别|不用|无需|不许|禁止|不能")
CONDITIONAL = re.compile(r"如果|假如|若是|只有|(?<!刚)才(?:能|可)?")
REPORT_NEGATION = re.compile(r"(?:不要|不用|无需|别|禁止)[^，。；;]{0,12}(?:报告|报表|图片|出图)")


class IntentPlanner(Protocol):
    async def parse(
        self,
        query: str,
        memory: ConversationState,
        budget: LLMBudget,
        patient_brief: str | None = None,
        *,
        prompt_binding: PromptBinding | None = None,
        prompt_usages: list[dict] | None = None,
    ) -> IntentDecision: ...


def clarification(text: str = "请明确要查询的内容、记录范围或要执行的动作。") -> IntentDecision:
    return IntentDecision(decision="clarify", decision_summary=text)


class ConservativePlanner:
    """明确模式供离线演示/失效兜底；不以单个关键词自动执行自由口语。"""

    async def parse(
        self,
        query: str,
        memory: ConversationState,
        budget: LLMBudget,
        patient_brief: str | None = None,
        *,
        prompt_binding: PromptBinding | None = None,
        prompt_usages: list[dict] | None = None,
    ) -> IntentDecision:
        memory = memory or ConversationState()
        q = query.strip()
        if re.fullmatch(r"(?:你好|您好|嗨|谢谢|感谢|再见|hello|hi)[！!。\.\s]*", q, re.I):
            return IntentDecision(
                decision="respond", decision_summary="你好，我可以协助查询训练、医生和场景信息。"
            )
        for device in ("iremo",):
            if device in q.lower():
                return IntentDecision(
                    decision="unsupported",
                    goals=[
                        Goal(
                            goal_id="g1",
                            kind="unsupported",
                            domain=device,
                            query_span=q,
                        )
                    ],
                    decision_summary="该设备的数据接口暂未接入。",
                )
        if CONDITIONAL.search(q) or any(mark in q for mark in ("“", "”", '"', "「", "」")):
            return clarification("这句话包含条件或引述，需要进一步明确执行范围。")
        clauses = re.split(
            r"(?:，|,|；|;|并且|同时|然后)\s*(?:并)?(?=查询|查看|解读|解释|生成|打开|关闭|前往|进入)",
            q,
        )
        if len(clauses) > 1 and all(c.strip() for c in clauses):
            goals = []
            for clause in clauses:
                decision = await self.parse(clause, memory, budget, patient_brief=patient_brief)
                if decision.decision != "execute":
                    return clarification("请分别明确各个查询或动作。")
                for goal in decision.goals:
                    goal.goal_id = f"g{len(goals) + 1}"
                    goals.append(goal)
            if len(goals) > 6:
                return clarification("本轮目标较多，请分批处理。")
            return IntentDecision(decision="execute", goals=goals)
        if "运营" in q and ("医院" in q or "机构" in q):
            match = re.search(r"(?:医院|机构)(?:编号|ID|id)?\s*(\d+)", q)
            if not match:
                return clarification("请提供要查询的医院编号或名称。")
            report = not REPORT_NEGATION.search(q) and bool(re.search(r"报告|报表|图片|出图", q))
            return IntentDecision(
                decision="execute",
                goals=[
                    Goal(
                        goal_id="g1",
                        kind="hospital_query",
                        domain="hospital",
                        query_span=q,
                        hospital=HospitalRequest(
                            hospital_id=int(match[1]),
                            output_mode="analysis_and_report" if report else "analysis",
                        ),
                    )
                ],
            )
        explicit_tour = bool(re.search(r"iretour", q, re.I))
        explicit_go = bool(re.search(r"irego", q, re.I))
        followup = bool(re.search(r"刚才|这次|本次|那次|上一次|前一次", q))
        tour_followup = (
            followup and memory.current_record and memory.current_record.domain == "iretour"
        )
        tour_page = (
            memory.history_seen
            and memory.history_domain == "iretour"
            and bool(re.fullmatch(r"(?:请)?(?:继续|下一页|再一页|上一页)[。\s]*", q))
        )
        if not explicit_go and (explicit_tour or tour_followup or tour_page):
            base_query = re.sub(r"iretour", "", q, flags=re.I).strip()
            if re.search(r"概览|概况|上下文", base_query):
                base_query = "查看患者概况"
            base_memory = memory.model_copy(deep=True)
            base_memory.history_domain = "irego"
            if base_memory.current_record:
                base_memory.current_record.domain = "irego"
            decision = await self.parse(base_query, base_memory, budget, patient_brief)
            for goal in decision.goals:
                if goal.kind != "irego" or goal.irego is None:
                    continue
                goal.kind, goal.domain, goal.query_span = "iretour", "iretour", q
                goal.iretour = IReTourRequest(**goal.irego.model_dump())
                for name, code in {
                    "直线初级": "straight_primary",
                    "直线高级": "straight_secondary",
                    "反应初级": "reaction_primary",
                    "反应高级": "reaction_secondary",
                    "平衡桥": "balance_bridge",
                    "振动": "vibration",
                    "横向": "transverse",
                    "跨步": "stride",
                    "侧向": "sideway",
                    "步态评估": "gait_assessment",
                }.items():
                    if name in q:
                        goal.iretour.activity_scope = code
                goal.irego = None
            return decision
        if re.fullmatch(r"(?:那)?(?:上一次|前一次)(?:呢)?[？?。\s]*", q):
            return IntentDecision(
                decision="execute",
                goals=[
                    Goal(
                        goal_id="g1",
                        kind="irego",
                        domain="irego",
                        query_span=q,
                        output="answer",
                        irego=IReGoRequest(
                            operation="session", selector=Selector(mode="previous_record")
                        ),
                    )
                ],
            )
        if re.fullmatch(r"(?:请)?(?:继续|下一页|再一页|上一页)[。\s]*", q):
            if not memory.history_seen:
                return clarification("请明确要继续的内容；当前会话尚未查询历史列表。")
            return IntentDecision(
                decision="execute",
                goals=[
                    Goal(
                        goal_id="g1",
                        kind="irego",
                        domain="irego",
                        query_span=q,
                        output="list",
                        irego=IReGoRequest(
                            operation="history",
                            selector=Selector(
                                mode="ordinal",
                                count=max(1, memory.history_page + (-1 if "上一页" in q else 1)),
                            ),
                        ),
                    )
                ],
            )
        # Only split a connector when an explicit action follows it; target names keep 和/再.
        chunks = re.split(rf"(?:然后|接着|再|并且|同时|并|，|,|；|;|。)\s*(?={ACTION})", q)
        if chunks and all(
            re.fullmatch(rf"(?:请|帮我|带我|先)*{ACTION}[^，。；;？?]*[。]?", c.strip())
            and not NEGATIVE.search(c)
            for c in chunks
        ):
            if len(chunks) > 6:
                return clarification("本轮动作较多，请分批明确需要处理的动作。")
            goals = [
                Goal(
                    goal_id=f"g{i + 1}",
                    kind="scene_action",
                    domain="scene",
                    query_span=c.strip(),
                    output="action",
                    after_goal_ids=[],
                )
                for i, c in enumerate(chunks)
            ]
            return IntentDecision(decision="execute", goals=goals)
        if (
            re.fullmatch(
                r"(?:请|查一下|查询|看看|查看|这里|当前空间|有|哪些|在线|医生|多少|谁|的|？|\?|。|\s)+",
                q,
            )
            and "医生" in q
        ):
            return IntentDecision(
                decision="execute",
                goals=[
                    Goal(
                        goal_id="g1",
                        kind="doctor_query",
                        domain="doctors",
                        query_span=q,
                        output="list",
                    )
                ],
            )
        if re.search(
            r"什么是|科普|注意事项|使用方法|怎么用|操作说明|帮助", q
        ) and not NEGATIVE.search(q):
            domain = (
                "help"
                if re.search(r"操作|帮助|使用方法|怎么用", q)
                else "product"
                if re.search(r"设备|产品", q)
                else "health"
            )
            return IntentDecision(
                decision="execute",
                goals=[Goal(goal_id="g1", kind="knowledge_query", domain=domain, query_span=q)],
            )
        # Explicit read requests are supported offline; negated artifacts stay excluded.
        read_request = re.search(
            r"查询|查一下|查看|解读|解释|怎么样|如何|生成|出图|训练|患者(?:信息|概况)", q
        )
        if not read_request or re.search(r"他说|例如|举例|提到|假设|讲个|写个", q):
            return clarification()
        forbidden_report = bool(REPORT_NEGATION.search(q))
        cleaned = REPORT_NEGATION.sub("", q)
        if NEGATIVE.search(cleaned):
            return clarification("已保留不执行的要求，请明确其余需要查询的内容。")
        if any(word in cleaned for word in ("打开", "关闭", "前往", "进入", "联系")):
            return clarification()
        is_report = not forbidden_report and bool(re.search(r"报告|报表|图片|出图|生成图|图表", q))
        selector = Selector(mode="latest_record")
        if re.search(r"刚才|这次|本次|那次", q):
            selector.mode = "current_ref"
        if re.search(r"最近.*(?:可用|能解读)", q):
            selector.mode = "latest_usable"
        if "上一次" in q or "前一次" in q:
            selector.mode = "previous_record"
        count = re.search(r"(?:最近|近)(\d+|[二三四五六七八九十两])次", q)
        if count:
            number = count.group(1)
            selector = Selector(
                mode="latest_count",
                count=int(number)
                if number.isdigit()
                else {
                    "二": 2,
                    "两": 2,
                    "三": 3,
                    "四": 4,
                    "五": 5,
                    "六": 6,
                    "七": 7,
                    "八": 8,
                    "九": 9,
                    "十": 10,
                }[number],
            )
        if re.search(r"趋势|进步|改善|下降|比较", q):
            operation = "trend"
            if selector.mode not in {"latest_count", "date_range"}:
                selector = Selector(mode="latest_count", count=4)
        elif "历史" in q or "既往" in q or "记录列表" in q:
            operation = "history"
        elif re.search(r"患者(?:信息|概况|背景)|个人(?:信息|概况)", q) and "训练" not in q:
            operation = "overview"
        elif re.search(r"训练|那次|刚才|这次|本次", q):
            operation = "session"
        else:
            return clarification()
        if is_report and not re.search(r"解读|解释|怎么样|分析|如何", q):
            output = "artifact"
        else:
            output = "answer_and_artifact" if is_report else "answer"
        topics = [
            word for word in ("速度", "时长", "步行", "完成", "坐站", "平衡", "游戏") if word in q
        ]
        return IntentDecision(
            decision="execute",
            goals=[
                Goal(
                    goal_id="g1",
                    kind="irego",
                    domain="irego",
                    query_span=q,
                    output=output,
                    excluded_outputs=["artifact"] if forbidden_report else [],
                    irego=IReGoRequest(
                        operation=operation,
                        selector=selector,
                        topics=topics,
                        need_artifact=is_report,
                        force_refresh=bool(re.search(r"刷新|重新", q)),
                    ),
                )
            ],
        )


class StructuredIntentPlanner:
    def __init__(
        self, model: Any, settings: Settings, prompts: PromptService | None = None
    ) -> None:
        self.model = model.with_structured_output(IntentDecision, include_raw=True)
        self.settings = settings
        self.prompts = prompts or PromptService()
        self.fallback = ConservativePlanner()

    async def parse(
        self,
        query: str,
        memory: ConversationState,
        budget: LLMBudget,
        patient_brief: str | None = None,
        *,
        prompt_binding: PromptBinding | None = None,
        prompt_usages: list[dict] | None = None,
    ) -> IntentDecision:
        binding = prompt_binding or self.prompts.bind(self.settings.prompt_bundle)
        usages = prompt_usages if prompt_usages is not None else []
        repair = None
        history = list(memory.turns[-6:])
        limit = min(
            self.settings.planner_input_tokens,
            self.settings.model_context_tokens - self.settings.planner_max_tokens - 512,
        )
        schema_cost = estimate_tokens(IntentDecision.model_json_schema())
        while True:
            context = {
                "query": query,
                "recent_turns": history,
                "record_candidates": ["current"] if memory.current_record else [],
                "record_domain": memory.current_record.domain if memory.current_record else None,
                "history_domain": memory.history_domain if memory.history_seen else None,
                "history_page": memory.history_page if memory.history_seen else None,
                "pending_goals": [g.model_dump(mode="json") for g in memory.pending_goals],
                "patient_brief": patient_brief or None,
            }
            rendered = self.prompts.render(
                binding, "intent.parse", IntentPromptInputs.model_validate(context)
            )
            messages = list(rendered.messages)
            if estimate_tokens(messages) + schema_cost <= limit:
                break
            if history:
                history.pop(0)
            else:
                return clarification("原话与必要上下文超出理解预算，请缩小本轮范围。")
        for attempt in range(2):
            if estimate_tokens(messages) + schema_cost > limit:
                break
            try:
                await budget.take()
                usages.append(rendered.usage(budget.calls))
                if repair:
                    usages.append(repair.usage(budget.calls))
                async with asyncio.timeout(self.settings.planner_timeout_seconds):
                    result = await self.model.ainvoke(messages)
                if isinstance(result, dict) and "parsed" in result:
                    raw = result.get("raw")
                    usage = getattr(raw, "usage_metadata", None)
                    if usage:
                        budget.usage.append(dict(usage))
                    result = result["parsed"]
                if result is None:
                    raise ValueError("missing structured result")
                return IntentDecision.model_validate(result)
            except (ValidationError, ValueError, TypeError):
                if attempt == 0:
                    repair = self.prompts.render(binding, "intent.repair", EmptyPromptInputs())
                    messages.extend(repair.messages)
                    continue
            except (TimeoutError, DomainError):
                break
            except Exception:
                # Provider failures never turn an arbitrary utterance into a tool command.
                break
        return await self.fallback.parse(query, memory, budget, patient_brief=patient_brief)
