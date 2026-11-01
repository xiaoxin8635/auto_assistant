"""mock 四服务接口测试（ADR-0006）：TestClient 直调 app（不起端口），覆盖正常流与错误映射。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import pytest

from mock.scenario_engine.engine import ScenarioEngine
from mock.scenario_engine.state_store import MockStateStore
from mock.services.cmdb import create_app as create_cmdb
from mock.services.logs import create_app as create_logs
from mock.services.monitor import create_app as create_monitor
from mock.services.ticket import create_app as create_ticket

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = PROJECT_ROOT / "mock" / "scenarios"
INVENTORY_PATH = PROJECT_ROOT / "mock" / "scenarios" / "hosts.yaml"

HOST = "host-web-01"
SERVICE = "log-agent"


class FakeClock:
    """可控假时钟（与引擎测试同款，保证确定性）。"""

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
def store(tmp_path: Path) -> MockStateStore:
    """临时 SQLite 状态库夹具（每个测试独立文件）。"""
    st = MockStateStore(tmp_path / "mock_state.sqlite3")
    st.init_schema()
    return st


def _make_engine(store: MockStateStore, clock: FakeClock) -> ScenarioEngine:
    """构建绑定临时状态库的引擎。

    Args:
        store: 临时状态库。
        clock: 假时钟。

    Returns:
        场景引擎。
    """
    return ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store, seed=42, clock=clock)


def test_monitor_health_and_metrics(store: MockStateStore) -> None:
    """monitor：健康探针与指标查询（注入后处于故障区间）。"""
    clock = FakeClock()
    engine = _make_engine(store, clock)
    engine.inject_fault("SC-CPU-001")
    clock.advance(30.0)
    client = TestClient(create_monitor(engine))

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "monitor"}

    resp = client.get("/metrics", params={"host_id": HOST, "metric": "cpu"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_id"] == HOST
    assert len(body["points"]) >= 3
    assert all(91.0 <= p["value"] <= 98.0 for p in body["points"][-3:])

    resp = client.get("/service_status", params={"host_id": HOST})
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


def test_monitor_inject_fix_reset_lifecycle(store: MockStateStore) -> None:
    """monitor 控制面：注入→修复→复位全生命周期经 HTTP 完成。"""
    engine = _make_engine(store, FakeClock())
    client = TestClient(create_monitor(engine))

    resp = client.post("/scenario/inject", json={"scenario_id": "SC-CPU-001"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "fault"

    resp = client.post(
        "/scenario/fix",
        json={
            "host_id": HOST,
            "action": "restart_service",
            "params": {"host_id": HOST, "service_name": SERVICE, "request_id": "req-1"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched"] is True
    assert body["effect"] == "recovered"

    resp = client.post("/scenario/reset")
    assert resp.status_code == 200
    assert engine.state_of(HOST) == "normal"


def test_monitor_recovery_check_endpoint(store: MockStateStore) -> None:
    """monitor 恢复判定端点：注入后未恢复，recovered 修复后判定恢复。"""
    clock = FakeClock()
    engine = _make_engine(store, clock)
    engine.inject_fault("SC-CPU-001")
    clock.advance(30.0)
    client = TestClient(create_monitor(engine))

    resp = client.get("/recovery_check", params={"host_id": HOST})
    assert resp.status_code == 200
    assert resp.json()["host_id"] == HOST
    assert resp.json()["recovered"] is False

    engine.apply_fix(
        HOST, "restart_service", {"host_id": HOST, "service_name": SERVICE, "request_id": "req-rc"}
    )
    resp = client.get("/recovery_check", params={"host_id": HOST})
    assert resp.status_code == 200
    assert resp.json()["recovered"] is True


def test_monitor_error_mapping(store: MockStateStore) -> None:
    """monitor 错误映射：未知场景 404、未知主机 404、错误体结构统一。"""
    engine = _make_engine(store, FakeClock())
    client = TestClient(create_monitor(engine))

    resp = client.post("/scenario/inject", json={"scenario_id": "SC-NOPE-999"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "MOCK-4001"
    assert "message" in body and "detail" in body

    resp = client.get("/metrics", params={"host_id": "host-ghost", "metric": "cpu"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "MOCK-4006"


def test_logs_service_shared_state(store: MockStateStore) -> None:
    """logs：经 monitor 注入后共享状态可检索到错误日志；未知主机 404。"""
    engine = _make_engine(store, FakeClock())
    engine.inject_fault("SC-CPU-001")
    client = TestClient(create_logs(engine))

    resp = client.get(
        "/search", params={"host_id": HOST, "service": SERVICE, "keyword": "retry loop"}
    )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["level"] == "ERROR"

    resp = client.get("/search", params={"host_id": "host-ghost"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "MOCK-4006"


def test_cmdb_service(store: MockStateStore) -> None:
    """cmdb：资产命中返回字段，查无此主机 404。"""
    engine = _make_engine(store, FakeClock())
    client = TestClient(create_cmdb(engine))

    resp = client.get("/host", params={"host_id": HOST})
    assert resp.status_code == 200
    assert resp.json()["service"] == SERVICE

    resp = client.get("/host", params={"host_id": "host-ghost"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "MOCK-4006"


def test_ticket_service_flow(store: MockStateStore) -> None:
    """ticket：创建→查询→闭环→查询闭环态；未知工单 404。"""
    client = TestClient(create_ticket(store))

    resp = client.post("/tickets", json={"title": "CPU 过载处理", "content": "SC-CPU-001"})
    assert resp.status_code == 201
    ticket = resp.json()
    assert ticket["status"] == "open"
    assert ticket["ticket_id"].startswith("TK-")

    resp = client.get(f"/tickets/{ticket['ticket_id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "CPU 过载处理"

    resp = client.post(f"/tickets/{ticket['ticket_id']}/close")
    assert resp.status_code == 200
    assert resp.json()["closed"] is True

    resp = client.get(f"/tickets/{ticket['ticket_id']}")
    assert resp.json()["status"] == "closed"

    resp = client.post("/tickets/TK-NOPE/close")
    assert resp.status_code == 404
    assert resp.json()["code"] == "MOCK-4006"
