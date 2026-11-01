"""CLI 测试：submit 同步执行、status 查询、audit 时间线渲染（注入替身组件，不起进程不连服务）。

服务面复用 httpx.MockTransport，模型面复用 FakeLLMAdapter——与 test_workflow 同款手法，
保证三命令的生产装配路径（build_parser → 子命令 handler → audit/graph）被真实走通。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from opsagent.access.cli import CliComponents, main
from opsagent.config import Settings
from opsagent.governance.audit import AuditLogger, TaskStatus
from opsagent.infra.db import Database
from opsagent.infra.llm import FakeLLMAdapter, LLMResponse, TokenUsage
from opsagent.tools.base import ServiceClient, ToolRegistry, ToolResult
from opsagent.tools.query_tools import QueryCmdbTool, QueryMetricsTool, SearchLogsTool
from opsagent.tools.recovery import RecoveryCheckTool
from opsagent.tools.restart_service import RestartServiceTool

ALERT = {
    "alert_id": "AL-2001",
    "host_id": "host-web-01",
    "service": "log-agent",
    "severity": "critical",
    "summary": "CPU 过载",
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
        "root_cause": "log-agent 日志轮转卡死",
        "confidence": 0.9,
        "fix_action": "restart_service",
        "fix_params": {"host_id": "host-web-01", "service_name": "log-agent"},
        "reason": "证据吻合",
    },
    ensure_ascii=False,
)


def _resp(content: str | None = None) -> LLMResponse:
    """构造一条 FakeLLM 预设响应。

    Args:
        content: 文本回复。

    Returns:
        LLMResponse。
    """
    return LLMResponse(
        content=content,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fake-model",
    )


@dataclass
class Backend:
    """假 mock 服务后端：查询正常应答，修复恢复可配置。"""

    recovery_ok: bool = True
    fix_requests: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """按路径路由应答。

        Args:
            request: httpx 请求。

        Returns:
            httpx 响应。
        """
        path = request.url.path
        if path == "/metrics":
            return httpx.Response(
                200, json={"points": [{"timestamp": 1_000_000.0, "value": 95.0}]}
            )
        if path == "/search":
            return httpx.Response(200, json={"entries": []})
        if path == "/host":
            return httpx.Response(
                200,
                json={"host_id": request.url.params.get("host_id", ""), "service": "log-agent"},
            )
        if path == "/scenario/fix":
            self.fix_requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"matched": True, "effect": "recovered", "state": "recovered", "at": 1_000_000.0},
            )
        if path == "/recovery_check":
            return httpx.Response(
                200,
                json={"recovered": self.recovery_ok, "checks": [], "evaluated_at": 1_000_000.0},
            )
        return httpx.Response(404, json={"code": "MOCK-4004", "message": "未知路径", "detail": {}})


def _make_comp(tmp_path: Path, llm: FakeLLMAdapter, backend: Backend) -> CliComponents:
    """装配注入替身的 CLI 组件（临时库 + MockTransport 注册表）。

    Args:
        tmp_path: pytest 临时目录。
        llm: FakeLLM。
        backend: 假 mock 后端。

    Returns:
        CliComponents。
    """
    settings = Settings(data_dir=tmp_path / "data")
    transport = httpx.MockTransport(backend.handler)

    def client_for(port: int) -> ServiceClient:
        """构建指向假服务的客户端。

        Args:
            port: mock 端口（仅路径语义）。

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
    return CliComponents(
        settings=settings,
        llm=llm,
        registry=registry,
        recovery=RecoveryCheckTool(client_for(8101)),
        audit=AuditLogger(Database(settings.data_dir / "opsagent.sqlite3")),
        cfg=settings.workflow,
    )


def _write_alert_file(tmp_path: Path, text: str) -> str:
    """写告警文件并返回路径字符串。

    Args:
        tmp_path: pytest 临时目录。
        text: 文件内容。

    Returns:
        文件路径字符串。
    """
    path = tmp_path / "alert.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_submit_closed(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """submit 闭环：exit 0，输出终态/根因/报告，任务主档落库为 closed。"""
    llm = FakeLLMAdapter(
        [
            _resp(TRIAGE_JSON),
            _resp("证据充分"),
            _resp(ANALYZE_JSON),
            _resp("根因为 log-agent 轮转卡死，已重启并验证恢复。"),
        ]
    )
    backend = Backend()
    comp = _make_comp(tmp_path, llm, backend)

    code = main(["submit", _write_alert_file(tmp_path, json.dumps(ALERT, ensure_ascii=False))], components=comp)
    out = capsys.readouterr().out

    assert code == 0
    assert "终态: closed" in out
    assert "根因: log-agent 日志轮转卡死" in out
    assert "报告: 根因为 log-agent 轮转卡死" in out
    task = comp.audit.list_tasks()[0]
    assert task["status"] == "closed"
    assert backend.fix_requests[0]["params"]["request_id"].startswith("task-")


def test_submit_inline_escalated(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """submit --inline：triage 输出非法 → exit 1，输出 WF-3001。"""
    comp = _make_comp(tmp_path, FakeLLMAdapter([_resp("这个告警很严重。")]), Backend())

    code = main(
        ["submit", "--inline", json.dumps(ALERT, ensure_ascii=False)], components=comp
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "终态: escalated" in captured.out
    assert "WF-3001" in captured.out


def test_submit_bad_input(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """submit 输入边界：坏 JSON / 非对象 / 缺输入 → exit 2 + ACC-7001。"""
    comp = _make_comp(tmp_path, FakeLLMAdapter([]), Backend())

    code = main(["submit", _write_alert_file(tmp_path, "不是 JSON")], components=comp)
    captured = capsys.readouterr()
    assert code == 2
    assert "ACC-7001" in captured.err

    code = main(["submit", "--inline", "[1, 2]"], components=comp)
    assert code == 2

    code = main(["submit"], components=comp)
    captured = capsys.readouterr()
    assert code == 2
    assert "ACC-7001" in captured.err


def test_status_list_and_detail(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """status：无参列任务表、有参出详情、未知任务 exit 2。"""
    comp = _make_comp(tmp_path, FakeLLMAdapter([]), Backend())
    comp.audit.create_task("task-a", ALERT, alert_id="AL-2001", scenario_id="SC-CPU-001")
    comp.audit.update_task_status("task-a", TaskStatus.CLOSED, final_report={"text": "已恢复"})

    code = main(["status"], components=comp)
    out = capsys.readouterr().out
    assert code == 0
    assert "task-a" in out and "SC-CPU-001" in out

    code = main(["status", "task-a"], components=comp)
    out = capsys.readouterr().out
    assert code == 0
    assert "status:     closed" in out
    assert "已恢复" in out

    code = main(["status", "task-ghost"], components=comp)
    captured = capsys.readouterr()
    assert code == 2
    assert "GOV-5002" in captured.err


def test_audit_timeline_and_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """audit：文本时间线跨表合并渲染；--json 输出原始数据；未知任务 exit 2。"""
    comp = _make_comp(tmp_path, FakeLLMAdapter([]), Backend())
    comp.audit.create_task("task-a", ALERT, alert_id="AL-2001")
    comp.audit.record_node("task-a", "triage", round_no=0, elapsed_ms=12.0)
    comp.audit.record_llm(
        "task-a", "triage", tier="fast", model="fake-model",
        messages=[{"role": "user", "content": "告警"}],
        prompt_tokens=10, completion_tokens=5, elapsed_ms=15.0,
    )
    comp.audit.record_tool(
        "task-a", "collect", "query_metrics", params={"host_id": "host-web-01"},
        result=ToolResult.ok({"points": []}, 3.0),
    )
    comp.audit.update_task_status("task-a", TaskStatus.CLOSED, final_report={"text": "报告文本"})

    code = main(["audit", "task-a"], components=comp)
    out = capsys.readouterr().out
    assert code == 0
    assert "[节点] triage" in out
    assert "[模型] triage tier=fast" in out
    assert "[工具] collect/query_metrics 成功" in out
    assert "[报告] 报告文本" in out

    code = main(["audit", "task-a", "--json"], components=comp)
    raw = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(raw.keys()) == {"task", "node_runs", "llm_calls", "tool_calls"}
    assert raw["task"]["task_id"] == "task-a"

    code = main(["audit", "task-ghost"], components=comp)
    captured = capsys.readouterr()
    assert code == 2
    assert "GOV-5002" in captured.err
