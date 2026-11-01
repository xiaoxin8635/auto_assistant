"""场景引擎行为测试（ADR-0006 跨进程版）：注入→诊断→修复→恢复判定全流程。

确定性保证：假时钟 + 固定种子 + SQLite 状态库（tmp 文件），无随机源。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mock.scenario_engine.engine import ScenarioEngine
from mock.scenario_engine.errors import (
    MOCK_4001_SCENARIO_NOT_FOUND,
    MOCK_4004_STATE_INVALID,
    MOCK_4006_HOST_NOT_FOUND,
    ScenarioError,
)
from mock.scenario_engine.metrics import SAMPLE_INTERVAL_SECONDS
from mock.scenario_engine.state_store import MockStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = PROJECT_ROOT / "mock" / "scenarios"
INVENTORY_PATH = PROJECT_ROOT / "mock" / "scenarios" / "hosts.yaml"

HOST = "host-web-01"
SERVICE = "log-agent"


class FakeClock:
    """可控假时钟：advance 手动推进，保证测试时间确定性。"""

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
def store(tmp_path: Path) -> MockStateStore:
    """临时 SQLite 状态库夹具（每个测试独立文件）。"""
    st = MockStateStore(tmp_path / "mock_state.sqlite3")
    st.init_schema()
    return st


@pytest.fixture
def engine(store: MockStateStore, clock: FakeClock) -> ScenarioEngine:
    """已注入 SC-CPU-001 故障的引擎夹具。"""
    eng = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store, seed=42, clock=clock)
    eng.inject_fault("SC-CPU-001")
    return eng


def test_scenario_loaded(engine: ScenarioEngine) -> None:
    """项目场景目录加载成功。"""
    assert "SC-CPU-001" in engine.scenario_ids()


def test_unknown_scenario_raises(engine: ScenarioEngine) -> None:
    """未知场景注入抛 MOCK-4001。"""
    with pytest.raises(ScenarioError) as exc_info:
        engine.inject_fault("SC-NOPE-999")
    assert exc_info.value.code == MOCK_4001_SCENARIO_NOT_FOUND


def test_cmdb_lookup(engine: ScenarioEngine) -> None:
    """资产查询命中与查无此主机语义。"""
    asset = engine.get_cmdb(HOST)
    assert asset is not None
    assert asset["service"] == SERVICE
    assert engine.get_cmdb("host-nope") is None


def test_normal_baseline_metrics(
    store: MockStateStore, clock: FakeClock
) -> None:
    """未注入主机：基线区间内的指标与服务状态。"""
    eng = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store, seed=42, clock=clock)
    points = eng.get_metrics(HOST, "cpu")
    assert len(points) >= 1
    for point in points:
        assert 14.0 <= point["value"] <= 31.0  # 基线 15~30（含 round 误差）
    assert eng.get_service_status(HOST) == "running"
    assert eng.state_of(HOST) == "normal"


def test_inject_fault_state_metrics_logs(
    engine: ScenarioEngine, clock: FakeClock
) -> None:
    """注入后：状态 FAULT、指标进入异常区间、错误日志可被关键字命中。"""
    clock.advance(30.0)
    assert engine.state_of(HOST) == "fault"
    points = engine.get_metrics(HOST, "cpu")
    assert len(points) >= 3
    for point in points[-3:]:  # 最近采样应处于故障区间 92~97
        assert 91.0 <= point["value"] <= 98.0

    hits = engine.search_logs(HOST, service=SERVICE, keyword="retry loop")
    assert len(hits) == 1
    assert hits[0]["level"] == "ERROR"

    all_logs = engine.search_logs(HOST)
    assert len(all_logs) == 3  # 2 条心跳 + 1 条错误


def test_inject_twice_rejected(engine: ScenarioEngine) -> None:
    """FAULT 态重复注入抛 MOCK-4004。"""
    with pytest.raises(ScenarioError) as exc_info:
        engine.inject_fault("SC-CPU-001")
    assert exc_info.value.code == MOCK_4004_STATE_INVALID


def test_unknown_host_raises(engine: ScenarioEngine) -> None:
    """未知主机查询抛 MOCK-4006。"""
    with pytest.raises(ScenarioError) as exc_info:
        engine.get_metrics("host-ghost", "cpu")
    assert exc_info.value.code == MOCK_4006_HOST_NOT_FOUND


def _restart_params(request_id: str | None = None) -> dict[str, Any]:
    """构造 restart_service 匹配参数。

    Args:
        request_id: 可选幂等键（验证额外键不影响匹配）。

    Returns:
        参数字典。
    """
    params: dict[str, Any] = {"host_id": HOST, "service_name": SERVICE}
    if request_id:
        params["request_id"] = request_id
    return params


def test_correct_fix_recovers_immediately(engine: ScenarioEngine) -> None:
    """正确修复：状态迁移 RECOVERED，锚点采样即刻满足恢复条件。"""
    result = engine.apply_fix(HOST, "restart_service", _restart_params("req-1"))
    assert result["matched"] is True
    assert result["effect"] == "recovered"
    assert result["state"] == "recovered"

    verdict = engine.evaluate_recovery(HOST)
    assert verdict["recovered"] is True
    assert all(check["passed"] for check in verdict["checks"])


def test_recovered_host_rejects_reinject(engine: ScenarioEngine) -> None:
    """RECOVERED 态不允许再次注入（MOCK-4004）。"""
    engine.apply_fix(HOST, "restart_service", _restart_params())
    with pytest.raises(ScenarioError) as exc_info:
        engine.inject_fault("SC-CPU-001")
    assert exc_info.value.code == MOCK_4004_STATE_INVALID


def test_mitigated_fix_keeps_fault_then_correct_fix_recovers(
    engine: ScenarioEngine,
) -> None:
    """部分缓解：仍 FAULT、指标改善但不达标、判不恢复；随后正确修复可恢复。"""
    mitigated = engine.apply_fix(
        HOST,
        "config_change",
        {"host_id": HOST, "service_name": SERVICE, "key": "rotate_interval", "value": 300},
    )
    assert mitigated["matched"] is True
    assert mitigated["effect"] == "mitigated"
    assert mitigated["state"] == "fault"

    # 缓解后即刻评估：锚点窗口只看缓解后指标（80~84），不得误判恢复
    verdict_mitigated = engine.evaluate_recovery(HOST)
    assert verdict_mitigated["recovered"] is False
    cpu_check = next(c for c in verdict_mitigated["checks"] if c["metric"] == "cpu")
    assert cpu_check["actual"] is not None
    assert 79.0 <= cpu_check["actual"] <= 85.0

    # 二次诊断后正确修复 → 恢复
    engine.apply_fix(HOST, "restart_service", _restart_params("req-2"))
    assert engine.evaluate_recovery(HOST)["recovered"] is True


def test_wrong_params_do_not_match(engine: ScenarioEngine) -> None:
    """参数不匹配（服务名错误）不产生任何状态效果。"""
    result = engine.apply_fix(
        HOST, "restart_service", {"host_id": HOST, "service_name": "mysql"}
    )
    assert result["matched"] is False
    assert engine.state_of(HOST) == "fault"

    # 不匹配的修复也不应设置恢复锚点（评估仍判未恢复）
    assert engine.evaluate_recovery(HOST)["recovered"] is False


def test_missing_param_does_not_match(engine: ScenarioEngine) -> None:
    """缺少必需参数键不匹配（防御 LLM 漏参）。"""
    result = engine.apply_fix(HOST, "restart_service", {"host_id": HOST})
    assert result["matched"] is False


def test_post_fix_window_excludes_fault_samples(
    engine: ScenarioEngine, clock: FakeClock
) -> None:
    """恢复后推进 2 个采样周期：窗口均值保持在基线区间（时间线正确性）。"""
    engine.apply_fix(HOST, "restart_service", _restart_params())
    clock.advance(SAMPLE_INTERVAL_SECONDS * 2)
    verdict = engine.evaluate_recovery(HOST)
    assert verdict["recovered"] is True
    cpu_check = next(c for c in verdict["checks"] if c["metric"] == "cpu")
    assert cpu_check["actual"] is not None
    assert cpu_check["actual"] <= 35.0


def test_reset_restores_clean_state(engine: ScenarioEngine) -> None:
    """复位：状态回 normal、日志与锚点清空、评估按无场景语义放行。"""
    engine.reset()
    assert engine.state_of(HOST) == "normal"
    assert engine.search_logs(HOST) == []
    assert engine.evaluate_recovery(HOST)["recovered"] is True


def test_state_survives_engine_reopen(
    store: MockStateStore, clock: FakeClock
) -> None:
    """跨进程语义：新引擎实例绑定同一状态库，立即看到已注入的故障态。"""
    eng1 = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store, seed=42, clock=clock)
    eng1.inject_fault("SC-CPU-001")

    eng2 = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store, seed=42, clock=clock)
    assert eng2.state_of(HOST) == "fault"
    assert eng2.search_logs(HOST, keyword="retry loop")
    # 实例 2 同样能推进状态迁移（修复在任一进程执行均有效）
    result = eng2.apply_fix(
        HOST, "restart_service", {"host_id": HOST, "service_name": SERVICE}
    )
    assert result["effect"] == "recovered"


def test_oracle_deterministic_across_stores(clock: FakeClock, tmp_path: Path) -> None:
    """确定性：同种子同时刻的两份独立状态库产生逐位一致的指标序列。"""
    store_a = MockStateStore(tmp_path / "a.sqlite3")
    store_b = MockStateStore(tmp_path / "b.sqlite3")
    store_a.init_schema()
    store_b.init_schema()
    eng_a = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store_a, seed=42, clock=clock)
    eng_b = ScenarioEngine(SCENARIOS_DIR, INVENTORY_PATH, store_b, seed=42, clock=clock)
    eng_a.inject_fault("SC-CPU-001")
    eng_b.inject_fault("SC-CPU-001")

    clock.advance(30.0)
    assert eng_a.get_metrics(HOST, "cpu") == eng_b.get_metrics(HOST, "cpu")
