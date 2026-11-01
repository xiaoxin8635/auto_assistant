"""工作流测试：五节点主链的闭环 / 转人工 / 预算熔断 / 工具容错（FakeLLM 脚本 + MockTransport）。

不依赖真实 mock 服务与真实 LLM：服务面用 httpx.MockTransport 按端口路由应答，
模型面用 FakeLLMAdapter 脚本队列——保证全链路确定性可断言。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from opsagent.config import WorkflowConfig
from opsagent.governance.audit import AuditLogger
from opsagent.infra.db import Database
from opsagent.infra.llm import FakeLLMAdapter, LLMResponse, TokenUsage, ToolCallOut
from opsagent.tools.base import ServiceClient, ToolRegistry
from opsagent.tools.query_tools import QueryCmdbTool, QueryMetricsTool, SearchLogsTool
from opsagent.tools.recovery import RecoveryCheckTool
from opsagent.tools.restart_service import RestartServiceTool
from opsagent.workflow.graph import build_diagnosis_graph, run_task

TASK_ID = "task-wf-001"
ALERT_RAW = {
    "alert_id": "AL-1001",
    "host_id": "host-web-01",
    "service": "log-agent",
    "metric": "cpu",
    "value": 95.5,
    "severity": "critical",
    "summary": "CPU 利用率超过 90%",
}
TRIAGE_JSON = json.dumps(
    {
        "host_id": "host-web-01",
        "service": "log-agent",
        "severity": "critical",
        "summary": "CPU 过载",
        "metric": "cpu",
        "value": 95.5,
    },
    ensure_ascii=False,
)
ANALYZE_JSON = json.dumps(
    {
        "root_cause": "log-agent 日志轮转卡死导致 CPU 打满",
        "confidence": 0.9,
        "fix_action": "restart_service",
        "fix_params": {"host_id": "host-web-01", "service_name": "log-agent"},
        "reason": "指标与日志证据吻合",
    },
    ensure_ascii=False,
)
FIX_RECOVERED = {"matched": True, "effect": "recovered", "state": "recovered", "at": 1_000_000.0}
RECOVERY_OK = {"recovered": True, "checks": [{"metric": "cpu", "passed": True}]}
RECOVERY_BAD = {"recovered": False, "checks": [{"metric": "cpu", "passed": False}]}


class MockBackend:
    """假 mock 服务后端：记录请求、按序返回预设修复/恢复结果。"""

    def __init__(
        self,
        *,
        fix_results: list[dict[str, Any]] | None = None,
        recovery_results: list[dict[str, Any]] | None = None,
        cmdb_missing: tuple[str, ...] = (),
    ) -> None:
        """初始化预设应答序列。

        Args:
            fix_results: /scenario/fix 按序应答（越界复用末项）。
            recovery_results: /recovery_check 按序应答（越界复用末项）。
            cmdb_missing: 视为不存在的主机 ID（返回 MOCK-4006）。
        """
        self.fix_results = fix_results or [FIX_RECOVERED]
        self.recovery_results = recovery_results or [RECOVERY_OK]
        self.cmdb_missing = cmdb_missing
        self.fix_requests: list[dict[str, Any]] = []
        self.fix_calls = 0
        self.recovery_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        """按路径路由应答。

        Args:
            request: httpx 请求。

        Returns:
            httpx 响应。
        """
        path = request.url.path
        if path == "/metrics":
            points = [{"timestamp": float(i), "value": 95.0} for i in range(6)]
            return httpx.Response(200, json={"points": points})
        if path == "/search":
            entries = [
                {"level": "ERROR", "message": "log rotate stuck in retry loop", "timestamp": 1_000_000.0}
            ]
            return httpx.Response(200, json={"entries": entries})
        if path == "/host":
            host_id = request.url.params.get("host_id", "")
            if host_id in self.cmdb_missing:
                return httpx.Response(
                    404, json={"code": "MOCK-4006", "message": "主机不存在", "detail": {}}
                )
            return httpx.Response(200, json={"host_id": host_id, "service": "log-agent"})
        if path == "/scenario/fix":
            self.fix_requests.append(json.loads(request.content))
            result = self.fix_results[min(self.fix_calls, len(self.fix_results) - 1)]
            self.fix_calls += 1
            return httpx.Response(200, json=result)
        if path == "/recovery_check":
            result = self.recovery_results[
                min(self.recovery_calls, len(self.recovery_results) - 1)
            ]
            self.recovery_calls += 1
            return httpx.Response(
                200,
                json={"host_id": request.url.params.get("host_id", ""), **result},
            )
        return httpx.Response(404, json={"code": "MOCK-4004", "message": "未知路径", "detail": {}})


def _resp(
    content: str | None = None,
    tool_calls: list[ToolCallOut] | None = None,
) -> LLMResponse:
    """构造一条 FakeLLM 预设响应（每条固定 15 token，预算测试可控）。

    Args:
        content: 文本回复。
        tool_calls: 工具调用列表。

    Returns:
        LLMResponse。
    """
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fake-model",
    )


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    """临时库审计器夹具。"""
    return AuditLogger(Database(tmp_path / "opsagent.sqlite3"))


def _build(
    audit: AuditLogger,
    llm: FakeLLMAdapter,
    backend: MockBackend,
    *,
    max_rounds: int = 5,
    budget: int = 10**6,
) -> Any:
    """装配完整工作流（图 + 注册表 + 恢复工具），全部指向 MockTransport。

    Args:
        audit: 审计器。
        llm: FakeLLM。
        backend: 假 mock 后端。
        max_rounds: 轮次上限。
        budget: token 预算。

    Returns:
        (编译图, WorkflowConfig)。
    """
    cfg = WorkflowConfig(max_rounds=max_rounds, token_budget_default=budget)
    transport = httpx.MockTransport(backend.handler)

    def client_for(port: int) -> ServiceClient:
        """构建指向假服务的客户端。

        Args:
            port: mock 端口（仅路径语义，不真实连接）。

        Returns:
            ServiceClient。
        """
        return ServiceClient(
            f"http://mock:{port}", max_retries=0, backoff_base=0.001, transport=transport
        )

    registry = ToolRegistry()
    registry.register(QueryMetricsTool(client_for(8101)))
    registry.register(SearchLogsTool(client_for(8102)))
    registry.register(QueryCmdbTool(client_for(8103)))
    registry.register(RestartServiceTool(client_for(8101)))
    recovery = RecoveryCheckTool(client_for(8101))
    graph = build_diagnosis_graph(llm, registry, audit, recovery, cfg=cfg)
    return graph, cfg


def test_happy_path_closed(audit: AuditLogger) -> None:
    """闭环主链路：triage→collect(带工具调用)→analyze→execute→validate 恢复→closed。"""
    llm = FakeLLMAdapter(
        [
            _resp(content=TRIAGE_JSON),
            _resp(
                tool_calls=[
                    ToolCallOut(
                        id="c1",
                        name="query_metrics",
                        arguments_json=json.dumps({"host_id": "host-web-01", "metric": "cpu"}),
                    )
                ]
            ),
            _resp(content="证据充分：CPU 持续 95%+，日志显示轮转卡死"),
            _resp(content=ANALYZE_JSON),
            _resp(content="根因为 log-agent 轮转卡死，已重启并验证恢复。"),
        ]
    )
    backend = MockBackend()
    graph, cfg = _build(audit, llm, backend)

    final = run_task(
        graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, scenario_id="SC-CPU-001", cfg=cfg
    )

    assert final["status"] == "closed"
    assert final["recovery"]["recovered"] is True
    assert final["report"]["text"].startswith("根因")

    task = audit.get_task(TASK_ID)
    assert task is not None
    assert task["status"] == "closed"
    assert task["closed_at"] is not None
    assert task["final_report"]["recovery"]["recovered"] is True

    timeline = audit.export_timeline(TASK_ID)
    assert len(timeline["llm_calls"]) == 5  # triage + collect×2 + analyze + report
    assert [c["tool_name"] for c in timeline["tool_calls"]] == [
        "query_metrics",
        "restart_service",
        "check_recovery",
    ]
    assert all(c["success"] == 1 for c in timeline["tool_calls"])
    assert backend.fix_requests[0]["params"]["request_id"] == f"{TASK_ID}-r0"

    # collect 的 function-calling 调用带工具 schema；triage 纯文本 fast 档
    assert llm.calls[0]["tier"] == "fast" and llm.calls[0]["tools"] is None
    assert llm.calls[1]["tier"] == "deep" and llm.calls[1]["tools"]


def test_triage_invalid_output_escalates(audit: AuditLogger) -> None:
    """triage 输出非 JSON → WF-3001 转人工，任务落审计终态。"""
    llm = FakeLLMAdapter([_resp(content="这个告警很严重，但我不打算输出 JSON。")])
    backend = MockBackend()
    graph, cfg = _build(audit, llm, backend)

    final = run_task(graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, cfg=cfg)

    assert final["status"] == "escalated"
    assert final["error"]["code"] == "WF-3001"
    assert audit.get_task(TASK_ID)["status"] == "escalated"
    assert backend.fix_calls == 0


def test_analyze_invalid_output_escalates(audit: AuditLogger) -> None:
    """analyze 输出缺 fix_action → WF-3001 转人工。"""
    llm = FakeLLMAdapter(
        [
            _resp(content=TRIAGE_JSON),
            _resp(content="证据已经足够"),
            _resp(content='{"root_cause": "内存泄漏", "confidence": 0.8}'),
        ]
    )
    backend = MockBackend()
    graph, cfg = _build(audit, llm, backend)

    final = run_task(graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, cfg=cfg)

    assert final["status"] == "escalated"
    assert final["error"]["code"] == "WF-3001"
    assert backend.fix_calls == 0


def test_max_rounds_escalates(audit: AuditLogger) -> None:
    """恢复始终未达成 → 轮次超限 WF-3002；每轮修复请求均落审计。"""
    llm = FakeLLMAdapter(
        [
            _resp(content=TRIAGE_JSON),
            _resp(content="先看看指标"),
            _resp(content=ANALYZE_JSON),
            _resp(content=ANALYZE_JSON),  # 第二轮 analyze
        ]
    )
    backend = MockBackend(
        fix_results=[FIX_RECOVERED, FIX_RECOVERED],
        recovery_results=[RECOVERY_BAD, RECOVERY_BAD],
    )
    graph, cfg = _build(audit, llm, backend, max_rounds=2)

    final = run_task(graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, cfg=cfg)

    assert final["status"] == "escalated"
    assert final["error"]["code"] == "WF-3002"
    assert final["round"] == 2
    assert backend.fix_calls == 2
    tool_calls = audit.export_timeline(TASK_ID)["tool_calls"]
    assert sum(1 for c in tool_calls if c["tool_name"] == "restart_service") == 2


def test_token_budget_escalates(audit: AuditLogger) -> None:
    """预算 10 < 单次响应 15 token → triage 后即熔断 WF-3003。"""
    llm = FakeLLMAdapter([_resp(content=TRIAGE_JSON)])
    backend = MockBackend()
    graph, cfg = _build(audit, llm, backend, budget=10)

    final = run_task(graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, cfg=cfg)

    assert final["status"] == "escalated"
    assert final["error"]["code"] == "WF-3003"
    assert final["token_used"] == 15
    assert backend.fix_calls == 0


def test_unknown_fix_action_escalates(audit: AuditLogger) -> None:
    """analyze 给出未注册动作 → execute 转人工（TOOL-1003 原样透传）。"""
    bad_plan = json.dumps(
        {
            "root_cause": "疑似宕机",
            "confidence": 0.5,
            "fix_action": "reboot_host",
            "fix_params": {"host_id": "host-web-01"},
            "reason": "猜测",
        },
        ensure_ascii=False,
    )
    llm = FakeLLMAdapter(
        [
            _resp(content=TRIAGE_JSON),
            _resp(content="证据足够"),
            _resp(content=bad_plan),
        ]
    )
    backend = MockBackend()
    graph, cfg = _build(audit, llm, backend)

    final = run_task(graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, cfg=cfg)

    assert final["status"] == "escalated"
    assert final["error"]["code"] == "TOOL-1003"
    assert backend.fix_calls == 0
    tool_calls = audit.export_timeline(TASK_ID)["tool_calls"]
    assert all(c["tool_name"] != "reboot_host" for c in tool_calls)


def test_collect_tool_failure_tolerated_and_deduped(audit: AuditLogger) -> None:
    """collect 工具报错不终止任务：错误作为证据回传模型；同签名重复调用证据去重。"""
    dup_call = ToolCallOut(
        id="c9",
        name="query_cmdb",
        arguments_json=json.dumps({"host_id": "host-ghost"}),
    )
    llm = FakeLLMAdapter(
        [
            _resp(content=TRIAGE_JSON),
            _resp(tool_calls=[dup_call, dup_call.model_copy(update={"id": "c10"})]),
            _resp(content="CMDB 查不到该主机，但指标证据已足够"),
            _resp(content=ANALYZE_JSON),
            _resp(content="已修复并验证恢复。"),
        ]
    )
    backend = MockBackend(cmdb_missing=("host-ghost",))
    graph, cfg = _build(audit, llm, backend)

    final = run_task(graph, audit, task_id=TASK_ID, alert_raw=ALERT_RAW, cfg=cfg)

    assert final["status"] == "closed"
    # 两次重复调用均执行并审计（success=0），但证据只留一条（签名去重）
    cmdb_calls = [
        c for c in audit.export_timeline(TASK_ID)["tool_calls"] if c["tool_name"] == "query_cmdb"
    ]
    assert len(cmdb_calls) == 2
    assert all(c["success"] == 0 and c["error_code"] == "MOCK-4006" for c in cmdb_calls)
    evidence = [e for e in final["evidence"] if e["tool"] == "query_cmdb"]
    assert len(evidence) == 1
    assert evidence[0]["error_code"] == "MOCK-4006"
