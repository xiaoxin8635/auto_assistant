"""场景数据模型单元测试：区间/时间窗解析与 SC-CPU-001 真实文件加载。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mock.scenario_engine.errors import MOCK_4005_DATA_INVALID, ScenarioError
from mock.scenario_engine.models import (
    ScenarioSpec,
    parse_metric_range,
    parse_window_seconds,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = PROJECT_ROOT / "mock" / "scenarios"


def test_parse_metric_range() -> None:
    """`低~高` 解析与乱序纠正。"""
    assert parse_metric_range("92~97") == (92.0, 97.0)
    assert parse_metric_range("97~92") == (92.0, 97.0)  # 乱序自动纠正


@pytest.mark.parametrize("bad", ["abc", "92", "92~97~99", ""])
def test_parse_metric_range_rejects_bad_format(bad: str) -> None:
    """非法区间字符串抛 ScenarioError(MOCK-4005)。"""
    with pytest.raises(ScenarioError) as exc_info:
        parse_metric_range(bad)
    assert exc_info.value.code == MOCK_4005_DATA_INVALID


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30s", 30.0), ("2m", 120.0), ("1h", 3600.0)],
)
def test_parse_window_seconds(text: str, expected: float) -> None:
    """时间窗 s/m/h 后缀解析。"""
    assert parse_window_seconds(text) == expected


@pytest.mark.parametrize("bad", ["5x", "m", ""])
def test_parse_window_seconds_rejects_bad_format(bad: str) -> None:
    """非法时间窗抛 ScenarioError(MOCK-4005)。"""
    with pytest.raises(ScenarioError) as exc_info:
        parse_window_seconds(bad)
    assert exc_info.value.code == MOCK_4005_DATA_INVALID


def test_load_real_scenario_sc_cpu_001() -> None:
    """真实场景文件加载：结构完整、修复选项恰好一个 recovered。"""
    import yaml

    raw = (SCENARIOS_DIR / "sc-cpu-001.yaml").read_text(encoding="utf-8")
    spec = ScenarioSpec.model_validate(yaml.safe_load(raw))
    assert spec.scenario_id == "SC-CPU-001"
    assert spec.fault.host == "host-web-01"
    assert spec.fault.service == "log-agent"
    effects = sorted(option.effect for option in spec.fix_options)
    assert effects == ["mitigated", "recovered"]
    assert len(spec.recovery_check) == 2
    assert spec.pre_inject_logs == 2


def test_scenario_without_recovered_fix_rejected() -> None:
    """没有 recovered 修复的场景（不可解）在模型校验期拒绝。"""
    base = {
        "scenario_id": "SC-BAD-001",
        "title": "不可解场景",
        "fault": {
            "type": "cpu_overload",
            "host": "host-web-01",
            "service": "log-agent",
            "metric_effect": {"cpu": "92~97"},
            "log_signature": "boom",
        },
        "pre_condition": {"cpu": "15~30"},
        "fix_options": [
            {
                "action": "config_change",
                "params": {"host_id": "host-web-01"},
                "effect": "mitigated",
                "partial_metric_effect": {"cpu": "80~84"},
            }
        ],
        "recovery_check": [{"metric": "cpu", "op": "<", "value": 80, "window": "2m"}],
    }
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(base)


def test_scenario_unknown_field_rejected() -> None:
    """多余字段（拼写错误防御）在模型校验期拒绝。"""
    base = {
        "scenario_id": "SC-BAD-002",
        "title": "多余字段场景",
        "fault_typo": {},  # 拼错的键
        "fault": {
            "type": "cpu_overload",
            "host": "host-web-01",
            "service": "log-agent",
            "metric_effect": {"cpu": "92~97"},
            "log_signature": "boom",
        },
        "pre_condition": {"cpu": "15~30"},
        "fix_options": [
            {
                "action": "restart_service",
                "params": {"host_id": "host-web-01"},
                "effect": "recovered",
            }
        ],
        "recovery_check": [{"metric": "cpu", "op": "<", "value": 80, "window": "2m"}],
    }
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(base)
