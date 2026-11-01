"""工作流全局状态定义（DESIGN §5.5.1 的 P1 子集；Checkpointer 相关持久化 P2 引入）。

LangGraph 合并语义：节点返回的 dict 按键覆盖写入状态；列表类字段由节点自行携带全量
（evidence / summaries 均为"读旧值 → 拷贝追加"模式，禁止原地修改）。
"""

from __future__ import annotations

from typing import Any, TypedDict


class DiagnosisState(TypedDict, total=False):
    """诊断任务全局状态（P1 五节点主链）。

    Attributes:
        task_id: 任务 ID（审计外键）。
        alert: {"raw": 原始告警, "structured": triage 结构化结果}。
        evidence: 已收集证据（[{tool, params, data, error_code}]，按签名去重累积）。
        hypotheses: 根因假设（P1 analyze 单轮直出，字段保留对齐 DESIGN §5.5.1）。
        root_cause: 确认的根因（{desc, confidence}）。
        fix_plan: 修复方案（{action, params}）。
        fix_result: 修复执行结果（mock /scenario/fix 响应体）。
        recovery: 恢复判定结果（/recovery_check 响应体）。
        report: 处理报告（{text, recovery}，闭环时写入审计 final_report）。
        round: 二次诊断轮次（validate 未恢复 → analyze 计一轮）。
        token_used: 累计 token（预算熔断数据源）。
        summaries: 各节点摘要（分层记忆雏形，P2 由模型生成）。
        status: running / closed / escalated / failed（非 running 即路由终点）。
        error: 终止原因（{code, message}）。
    """

    task_id: str
    alert: dict[str, Any]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    root_cause: dict[str, Any] | None
    fix_plan: dict[str, Any] | None
    fix_result: dict[str, Any] | None
    recovery: dict[str, Any] | None
    report: dict[str, Any] | None
    round: int
    token_used: int
    summaries: list[str]
    status: str
    error: dict[str, Any] | None


def initial_state(task_id: str, alert_raw: dict[str, Any]) -> DiagnosisState:
    """构造任务初始状态。

    Args:
        task_id: 任务 ID。
        alert_raw: 原始告警 JSON。

    Returns:
        完整初始状态（所有键显式赋值）。
    """
    return {
        "task_id": task_id,
        "alert": {"raw": alert_raw},
        "evidence": [],
        "hypotheses": [],
        "root_cause": None,
        "fix_plan": None,
        "fix_result": None,
        "recovery": None,
        "report": None,
        "round": 0,
        "token_used": 0,
        "summaries": [],
        "status": "running",
        "error": None,
    }


__all__ = ["DiagnosisState", "initial_state"]
