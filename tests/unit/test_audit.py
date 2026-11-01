"""治理审计测试：四表写入、时间线导出、外键约束与任务状态机（假时钟确定性）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opsagent.errors import GOV_5002_TASK_NOT_FOUND, INF_8001_DB_OPERATION_FAILED, OpsAgentError
from opsagent.governance.audit import AuditLogger, TaskStatus
from opsagent.infra.db import Database
from opsagent.tools.base import ToolResult

TASK_ID = "task-0001"


class FakeClock:
    """可控假时钟：advance 手动推进，保证 created_at 时序确定。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        """初始化起始时刻。

        Args:
            start: 初始 epoch 秒。
        """
        self.now = start

    def __call__(self) -> float:
        """返回当前时刻。

        Returns:
            当前 epoch 秒。
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """推进时钟。

        Args:
            seconds: 推进秒数。
        """
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    """假时钟夹具。"""
    return FakeClock()


@pytest.fixture
def audit(tmp_path: Path, clock: FakeClock) -> AuditLogger:
    """绑定临时库与假时钟的审计器（构造时幂等建表）。"""
    return AuditLogger(Database(tmp_path / "opsagent.sqlite3"), clock=clock)


def _make_task(audit: AuditLogger) -> None:
    """创建测试任务主档。

    Args:
        audit: 审计器。
    """
    audit.create_task(
        TASK_ID,
        {"alert_id": "AL-9", "host": "host-web-01", "metric": "cpu", "value": 95},
        alert_id="AL-9",
        scenario_id="SC-CPU-001",
    )


def test_create_and_get_task(audit: AuditLogger) -> None:
    """主档创建与读取：JSON 列还原为对象，字段完整。"""
    _make_task(audit)
    task = audit.get_task(TASK_ID)
    assert task is not None
    assert task["status"] == TaskStatus.RUNNING.value
    assert task["alert_raw"]["host"] == "host-web-01"
    assert task["scenario_id"] == "SC-CPU-001"
    assert task["closed_at"] is None


def test_get_missing_task_returns_none(audit: AuditLogger) -> None:
    """查询不存在的任务返回 None（查询语义）。"""
    assert audit.get_task("nope") is None


def test_update_terminal_status_sets_closed_at_and_report(
    audit: AuditLogger, clock: FakeClock
) -> None:
    """终态更新：closed_at 落库、final_report round-trip。"""
    _make_task(audit)
    clock.advance(120.0)
    report = {"root_cause": "log-agent 失控", "actions": ["restart_service"]}
    audit.update_task_status(TASK_ID, TaskStatus.CLOSED, final_report=report)

    task = audit.get_task(TASK_ID)
    assert task is not None
    assert task["status"] == TaskStatus.CLOSED.value
    assert task["closed_at"] == clock.now
    assert task["final_report"] == report


def test_non_terminal_update_keeps_closed_at_null(audit: AuditLogger) -> None:
    """非终态流转（running→waiting_human）不落 closed_at。"""
    _make_task(audit)
    audit.update_task_status(TASK_ID, TaskStatus.WAITING_HUMAN)
    task = audit.get_task(TASK_ID)
    assert task is not None
    assert task["status"] == TaskStatus.WAITING_HUMAN.value
    assert task["closed_at"] is None


def test_update_unknown_task_raises(audit: AuditLogger) -> None:
    """更新不存在的任务抛 GOV-5002。"""
    with pytest.raises(OpsAgentError) as exc_info:
        audit.update_task_status("ghost", TaskStatus.CLOSED)
    assert exc_info.value.code == GOV_5002_TASK_NOT_FOUND


def test_record_tool_from_tool_result(audit: AuditLogger) -> None:
    """工具审计：success 映射 0/1，结果载荷与错误码落库。"""
    _make_task(audit)
    ok = ToolResult.ok({"points": []}, elapsed_ms=12.5)
    bad = ToolResult.fail("MOCK-4006", "主机不存在", elapsed_ms=3.0)
    audit.record_tool(TASK_ID, "collect", "query_metrics", params={"host_id": "h"}, result=ok)
    audit.record_tool(TASK_ID, "collect", "query_cmdb", params={"host_id": "x"}, result=bad)

    timeline = audit.export_timeline(TASK_ID)
    calls = timeline["tool_calls"]
    assert calls[0]["success"] == 1 and calls[0]["error_code"] is None
    assert calls[0]["result"] == {"points": []}
    assert calls[0]["elapsed_ms"] == 12.5
    assert calls[1]["success"] == 0
    assert calls[1]["error_code"] == "MOCK-4006"
    assert calls[1]["params"] == {"host_id": "x"}


def test_record_llm_and_node(audit: AuditLogger) -> None:
    """LLM 与节点审计：token/模型版本/轮次完整落库。"""
    _make_task(audit)
    audit.record_llm(
        TASK_ID,
        "triage",
        tier="fast",
        model="deepseek-chat",
        messages=[{"role": "user", "content": "告警内容"}],
        response_text='{"host": "host-web-01"}',
        prompt_tokens=120,
        completion_tokens=30,
        elapsed_ms=800.0,
    )
    audit.record_node(
        TASK_ID, "triage", round_no=1, input={"alert_id": "AL-9"}, output={"host": "host-web-01"}, elapsed_ms=810.0
    )

    timeline = audit.export_timeline(TASK_ID)
    llm = timeline["llm_calls"][0]
    assert llm["tier"] == "fast" and llm["model"] == "deepseek-chat"
    assert llm["prompt_tokens"] == 120 and llm["completion_tokens"] == 30
    assert llm["messages"][0]["content"] == "告警内容"
    node = timeline["node_runs"][0]
    assert node["round_no"] == 1
    assert node["input"] == {"alert_id": "AL-9"}
    assert node["output"] == {"host": "host-web-01"}


def test_export_timeline_missing_task_raises(audit: AuditLogger) -> None:
    """导出不存在的任务抛 GOV-5002。"""
    with pytest.raises(OpsAgentError) as exc_info:
        audit.export_timeline("ghost")
    assert exc_info.value.code == GOV_5002_TASK_NOT_FOUND


def test_export_timeline_order_and_parse(audit: AuditLogger, clock: FakeClock) -> None:
    """时间线导出：按时间升序、JSON 全部还原为对象。"""
    _make_task(audit)
    audit.record_node(TASK_ID, "triage", elapsed_ms=10.0)
    clock.advance(1.0)
    audit.record_tool(TASK_ID, "collect", "query_metrics", params={"host_id": "h"}, result=ToolResult.ok([], 5.0))
    clock.advance(1.0)
    audit.record_node(TASK_ID, "collect", round_no=1, input={"evidence": []}, elapsed_ms=20.0)

    timeline = audit.export_timeline(TASK_ID)
    # 三张明细表各自内部时间升序（跨表合并排序由 CLI 渲染时间线时负责）
    node_times = [entry["created_at"] for entry in timeline["node_runs"]]
    tool_times = [entry["created_at"] for entry in timeline["tool_calls"]]
    assert node_times == sorted(node_times) == [clock.now - 2.0, clock.now]
    assert tool_times == [clock.now - 1.0]
    assert timeline["tool_calls"][0]["params"] == {"host_id": "h"}  # dict 而非 JSON 字符串
    assert len(timeline["node_runs"]) == 2


def test_foreign_key_violation_loud(audit: AuditLogger) -> None:
    """主档缺失时写明细被外键约束拦截（INF-8001 快速失败，不允许孤儿审计）。"""
    with pytest.raises(OpsAgentError) as exc_info:
        audit.record_node("ghost", "triage")
    assert exc_info.value.code == INF_8001_DB_OPERATION_FAILED


def test_list_tasks_filter_and_order(audit: AuditLogger, clock: FakeClock) -> None:
    """任务列表：状态过滤生效，时间倒序。"""
    audit.create_task("task-a", {"alert": 1})
    clock.advance(1.0)
    audit.create_task("task-b", {"alert": 2}, status=TaskStatus.QUEUED)
    audit.update_task_status("task-b", TaskStatus.FAILED)

    assert [t["task_id"] for t in audit.list_tasks()] == ["task-b", "task-a"]
    failed = audit.list_tasks(status=TaskStatus.FAILED)
    assert len(failed) == 1 and failed[0]["task_id"] == "task-b"
