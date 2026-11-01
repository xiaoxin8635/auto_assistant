"""端到端集成测试：真实 HTTP 全链路（4 个 uvicorn 服务 + tools/audit/workflow 真装配），仅模型面用 FakeLLM 脚本化。

与单测的区别：ServiceClient 走真实 TCP（ephemeral 端口），场景引擎经 HTTP 注入/修复，
恢复判定走 /recovery_check 端点——验收"生产装配路径"本身可跑通（DESIGN §5.2 / §5.5）。
真实 LLM 的验收由 CLI + DeepSeek 手工执行（provider 切换后），不进自动化测试（确定性/成本）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from mock.services.cmdb import create_app as create_cmdb
from mock.services.logs import create_app as create_logs
from mock.services.monitor import create_app as create_monitor
from mock.scenario_engine.engine import ScenarioEngine
from mock.scenario_engine.state_store import MockStateStore
from mock.services.ticket import create_app as create_ticket
from opsagent.config import MockConfig, Settings
from opsagent.governance.audit import AuditLogger
from opsagent.infra.db import Database
from opsagent.infra.llm import FakeLLMAdapter, LLMResponse, TokenUsage, ToolCallOut
from opsagent.tools import build_default_registry
from opsagent.tools.recovery import build_recovery_tool
from opsagent.workflow.graph import build_diagnosis_graph, run_task

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = PROJECT_ROOT / "mock" / "scenarios"
INVENTORY_PATH = PROJECT_ROOT / "mock" / "scenarios" / "hosts.yaml"
HOST = "host-web-01"
SERVICE = "log-agent"
SCENARIO_ID = "SC-CPU-001"

ALERT_RAW = {
    "alert_id": "AL-E2E-001",
    "host_id": HOST,
    "service": SERVICE,
    "metric": "cpu",
    "value": 95.5,
    "severity": "critical",
    "summary": "CPU 利用率持续超过 90%",
}


class EphemeralServer:
    """后台 uvicorn 服务器（port=0 由内核分配端口，测试结束自动停止）。"""

    def __init__(self, app: Any) -> None:
        """绑定应用并准备服务器。

        Args:
            app: ASGI 应用。
        """
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> int:
        """启动并等待就绪。

        Returns:
            实际监听端口。
        """
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn 启动超时")
            time.sleep(0.05)
        assert self._server.servers, "uvicorn 无监听套接字"
        self.port = int(self._server.servers[0].sockets[0].getsockname()[1])
        return self.port

    def stop(self) -> None:
        """触发优雅退出并等待线程结束。"""
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class FakeClock:
    """可控假时钟（与单测同款）：采样按 10s 网格生成，假时钟保证注入后立刻有故障采样可查。"""

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
    """共享假时钟夹具（引擎与测试同源，注入后可精确推进）。"""
    return FakeClock()


@pytest.fixture
def mock_stack(tmp_path: Path, clock: FakeClock) -> dict[str, int]:
    """四个 mock 服务真实起在 ephemeral 端口，共享同一场景引擎（SQLite 状态库临时文件）。"""
    store = MockStateStore(tmp_path / "mock_state.sqlite3")
    store.init_schema()
    engine = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store, seed=42, clock=clock)
    servers = {
        name: EphemeralServer(factory(engine))
        for name, factory in [
            ("monitor", create_monitor),
            ("logs", create_logs),
            ("cmdb", create_cmdb),
            ("ticket", create_ticket),
        ]
    }
    ports = {name: srv.start() for name, srv in servers.items()}
    try:
        yield ports
    finally:
        for srv in servers.values():
            srv.stop()


def _resp(content: str | None = None, tool_calls: list[ToolCallOut] | None = None) -> LLMResponse:
    """构造一条 FakeLLM 预设响应。

    Args:
        content: 文本回复。
        tool_calls: 工具调用列表。

    Returns:
        LLMResponse。
    """
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(prompt_tokens=120, completion_tokens=40, total_tokens=160),
        model="fake-model",
    )


def test_e2e_closed_over_real_http(
    mock_stack: dict[str, int], clock: FakeClock, tmp_path: Path
) -> None:
    """全链路闭环：HTTP 注入故障 → 工作流（真实 HTTP 工具调用 + 修复 + 恢复判定）→ closed。"""
    # 1) 生产装配路径：Settings 指向真实端口，registry/recovery 经真实 HTTP 调 mock
    settings = Settings(
        data_dir=tmp_path / "data",
        mock=MockConfig(
            host="127.0.0.1",
            monitor_port=mock_stack["monitor"],
            logs_port=mock_stack["logs"],
            cmdb_port=mock_stack["cmdb"],
            ticket_port=mock_stack["ticket"],
        ),
    )
    audit = AuditLogger(Database(settings.data_dir / "opsagent.sqlite3"))
    llm = FakeLLMAdapter(
        [
            _resp(
                json.dumps(
                    {
                        "host_id": HOST,
                        "service": SERVICE,
                        "severity": "critical",
                        "summary": "CPU 过载",
                        "metric": "cpu",
                        "value": 95.5,
                    },
                    ensure_ascii=False,
                )
            ),
            _resp(
                tool_calls=[
                    ToolCallOut(
                        id="c1",
                        name="query_metrics",
                        arguments_json=json.dumps({"host_id": HOST, "metric": "cpu"}),
                    )
                ]
            ),
            _resp(content="CPU 持续 95%+，证据充分"),
            _resp(
                json.dumps(
                    {
                        "root_cause": "log-agent 日志轮转卡死导致 CPU 打满",
                        "confidence": 0.9,
                        "fix_action": "restart_service",
                        "fix_params": {"host_id": HOST, "service_name": SERVICE},
                        "reason": "指标与日志证据吻合",
                    },
                    ensure_ascii=False,
                )
            ),
            _resp(content="根因为 log-agent 轮转卡死，已重启服务并验证恢复。"),
        ]
    )
    cfg = settings.workflow
    graph = build_diagnosis_graph(
        llm, build_default_registry(settings), audit, build_recovery_tool(settings), cfg=cfg
    )

    # 2) 经 HTTP 注入故障（评测控制面同款路径）
    inject = httpx.post(
        f"http://127.0.0.1:{mock_stack['monitor']}/scenario/inject",
        json={"scenario_id": SCENARIO_ID},
        timeout=5.0,
    )
    assert inject.status_code == 200
    assert inject.json()["state"] == "fault"
    clock.advance(30.0)  # 推过采样网格，保证回看窗口尾部有 3 个故障采样点（与单测同款）

    # 注入后恢复判定应为 False（故障未修复）
    pre = httpx.get(
        f"http://127.0.0.1:{mock_stack['monitor']}/recovery_check",
        params={"host_id": HOST},
        timeout=5.0,
    )
    assert pre.json()["recovered"] is False

    # 3) 跑完整工作流 → closed
    final = run_task(
        graph, audit, task_id="task-e2e-001", alert_raw=ALERT_RAW, scenario_id=SCENARIO_ID, cfg=cfg
    )

    assert final["status"] == "closed"
    assert final["recovery"]["recovered"] is True
    assert final["fix_result"]["effect"] == "recovered"
    assert final["report"]["text"]

    # 4) 审计时间线完整：LLM 5 次（triage/collect×2/analyze/report）+ 工具 3 次
    timeline = audit.export_timeline("task-e2e-001")
    assert timeline["task"]["status"] == "closed"
    assert len(timeline["llm_calls"]) == 5
    assert [c["tool_name"] for c in timeline["tool_calls"]] == [
        "query_metrics",
        "restart_service",
        "check_recovery",
    ]
    # collect 拿到的指标是真实 mock 数据：尾部 3 个点处于故障区间（基线在前，故障在后）
    metric_evidence = [e for e in final["evidence"] if e["tool"] == "query_metrics"]
    assert metric_evidence
    points = metric_evidence[0]["data"]["points"]
    assert len(points) >= 3
    assert all(91.0 <= p["value"] <= 98.0 for p in points[-3:])
