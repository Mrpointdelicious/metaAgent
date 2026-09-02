"""
创建日期：2026-08-29
文件功能：装配计划、领域执行、上下文编译和文案验证节点形成主 LangGraph。
"""

from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from meta_agent.context.compiler import ContextCompiler
from meta_agent.evidence.store import EvidenceRepository
from meta_agent.graph.state import AgentState
from meta_agent.orchestration.planner import DeterministicTaskPlanner, TaskName
from meta_agent.orchestration.validator import PlanValidator
from meta_agent.validation.claims import ClaimValidator
from meta_agent.workflows.irego import IReGoWorkflow


def build_agent_graph(
    checkpointer: BaseCheckpointSaver[Any],
    planner: DeterministicTaskPlanner,
    plan_validator: PlanValidator,
    workflow: IReGoWorkflow,
    evidence_repository: EvidenceRepository,
    context_compiler: ContextCompiler,
    claim_validator: ClaimValidator,
) -> Any:
    """构建并编译最小可运行图。"""

    async def plan_tasks(state: AgentState) -> dict[str, Any]:
        plan = planner.plan(state["query"])
        tasks = plan_validator.validate(plan)
        return {
            "tasks": list(tasks),
            "requested_output": plan.requested_output,
        }

    async def execute_domain_workflow(state: AgentState) -> dict[str, Any]:
        tasks = cast(tuple[TaskName, ...], tuple(state.get("tasks") or []))
        outcome = await workflow.execute(state["patient_id"], tasks)
        evidence = await evidence_repository.save(
            scope_hash=state["scope_hash"],
            source="irego_rehab_workflow",
            payload=outcome.raw_payload,
        )
        return {
            "result_ref": evidence.result_ref,
            "facts": outcome.facts,
            "patient_message": outcome.patient_message,
            "image_urls": outcome.image_urls,
            "mode_commands": outcome.mode_commands,
        }

    async def compose_response(state: AgentState) -> dict[str, Any]:
        facts = state.get("facts") or {}
        compact_context = context_compiler.compile(state["query"], facts)
        response_text = claim_validator.validate(state.get("patient_message") or "")
        return {
            "compact_context": compact_context,
            "response_text": response_text,
        }

    builder = StateGraph(AgentState)
    builder.add_node("plan_tasks", plan_tasks)
    builder.add_node("execute_domain_workflow", execute_domain_workflow)
    builder.add_node("compose_response", compose_response)
    builder.add_edge(START, "plan_tasks")
    builder.add_edge("plan_tasks", "execute_domain_workflow")
    builder.add_edge("execute_domain_workflow", "compose_response")
    builder.add_edge("compose_response", END)
    return builder.compile(checkpointer=checkpointer)
