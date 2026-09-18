"""
创建日期：2026-09-12
文件功能：共享 LangGraph 生命周期；Checkpoint 仅保存运行编号和阶段。
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from meta_agent.application.context import RunContext


class LifecycleState(TypedDict):
    run_id: str
    stage: str


def build_runtime_graph(application, checkpointer, store):
    async def plan(state: LifecycleState, runtime: Runtime[RunContext]):
        await application.plan(runtime.context)
        return {"stage": "planned"}

    async def execute(state: LifecycleState, runtime: Runtime[RunContext]):
        await application.execute_plan(runtime.context)
        return {"stage": "executed"}

    graph = StateGraph(LifecycleState, context_schema=RunContext)
    graph.add_node("plan", plan)
    graph.add_node("execute", execute)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", END)
    return graph.compile(checkpointer=checkpointer, store=store)
