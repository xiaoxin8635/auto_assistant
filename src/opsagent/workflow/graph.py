"""工作流装配：StateGraph 五节点主链 + run_task 编排器（DESIGN §5.5.2 P1）。

拓扑（P1）：
    START → triage → collect → analyze → execute → validate
                                     ↑              │（未恢复 & 轮次未满）
                                     └──────────────┘
    任意节点将 status 置为非 running（closed/escalated/failed）即路由收敛到 END。
    check_recovery 不注册进 LLM 工具集，由 validate 节点直接持有调用。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from opsagent.config import WorkflowConfig
from opsagent.governance.audit import AuditLogger, TaskStatus
from opsagent.infra.llm import LLMAdapter
from opsagent.tools.base import BaseTool, ToolRegistry
from opsagent.workflow.nodes import (
    make_analyze,
    make_collect,
    make_execute,
    make_triage,
    make_validate,
)
from opsagent.workflow.state import DiagnosisState, initial_state


def _route_to(next_node: str) -> Any:
    """构造条件路由函数：status 为 running 走下一节点，否则收敛 END。

    Args:
        next_node: running 状态时的下游节点名。

    Returns:
        LangGraph 条件边函数。
    """

    def route(state: DiagnosisState) -> str:
        """按状态路由。

        Args:
            state: 当前全局状态。

        Returns:
            "end" 或下游节点名。
        """
        return "end" if state.get("status") != "running" else next_node

    return route


def build_diagnosis_graph(
    llm: LLMAdapter,
    registry: ToolRegistry,
    audit: AuditLogger,
    recovery_tool: BaseTool,
    *,
    cfg: WorkflowConfig,
) -> Any:
    """装配五节点诊断主链图。

    Args:
        llm: LLM 适配器（triage/报告 fast 档，collect/analyze deep 档）。
        registry: 工具注册表（collect 自动只取低危；execute 使用全量）。
        audit: 审计器。
        recovery_tool: 恢复判定工具（工作流内部持有）。
        cfg: 工作流配置。

    Returns:
        编译后的 LangGraph 图。
    """
    graph = StateGraph(DiagnosisState)
    graph.add_node("triage", make_triage(llm, audit, cfg))
    graph.add_node("collect", make_collect(llm, registry, audit, cfg))
    graph.add_node("analyze", make_analyze(llm, audit, cfg))
    graph.add_node("execute", make_execute(registry, audit))
    graph.add_node("validate", make_validate(llm, recovery_tool, audit, cfg))

    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage", _route_to("collect"), {"collect": "collect", "end": END}
    )
    graph.add_conditional_edges(
        "collect", _route_to("analyze"), {"analyze": "analyze", "end": END}
    )
    graph.add_conditional_edges(
        "analyze", _route_to("execute"), {"execute": "execute", "end": END}
    )
    graph.add_conditional_edges(
        "execute", _route_to("validate"), {"validate": "validate", "end": END}
    )
    graph.add_conditional_edges(
        "validate", _route_to("analyze"), {"analyze": "analyze", "end": END}
    )
    return graph.compile()


def run_task(
    graph: Any,
    audit: AuditLogger,
    *,
    task_id: str,
    alert_raw: dict[str, Any],
    scenario_id: str | None = None,
    cfg: WorkflowConfig | None = None,
) -> dict[str, Any]:
    """任务编排入口：建主档 → 跑图 → 回写终态与处理报告。

    Args:
        graph: build_diagnosis_graph 编译产物。
        audit: 审计器。
        task_id: 全局唯一任务 ID。
        alert_raw: 原始告警 JSON。
        scenario_id: 关联场景 ID（评测回溯用，可选）。
        cfg: 工作流配置（仅用于计算 recursion_limit；图内节点已在装配期绑定配置）。

    Returns:
        图运行的最终状态（含 status / error / report）。
    """
    audit.create_task(task_id, alert_raw, alert_id=alert_raw.get("alert_id"), scenario_id=scenario_id)
    max_rounds = (cfg or WorkflowConfig()).max_rounds
    final = graph.invoke(
        initial_state(task_id, alert_raw),
        config={"recursion_limit": max_rounds * 6 + 20},
    )
    audit.update_task_status(
        task_id,
        TaskStatus(final["status"]),
        final_report=final.get("report"),
    )
    return final


__all__ = ["build_diagnosis_graph", "run_task"]
