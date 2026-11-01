"""工作流层：LangGraph 五节点诊断主链（P1）；假设/验证循环、Checkpointer 持久化 P2 扩展本包。

对外入口：build_diagnosis_graph（装配）+ run_task（建档→跑图→回写终态）。
"""

from opsagent.workflow.graph import build_diagnosis_graph, run_task
from opsagent.workflow.state import DiagnosisState, initial_state

__all__ = ["DiagnosisState", "build_diagnosis_graph", "initial_state", "run_task"]
