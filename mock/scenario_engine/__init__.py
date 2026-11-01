"""场景引擎：故障状态机 + 确定性指标 oracle + SQLite 共享状态库（ADR-0006）。

四个 mock 服务进程各自实例化 ScenarioEngine，动态状态经 MockStateStore 共享，
任意进程写入后其余进程立即可见；恢复判定不过 LLM。
"""

from mock.scenario_engine.engine import DEFAULT_BASELINE_RANGES, DEFAULT_MOCK_SEED, ScenarioEngine
from mock.scenario_engine.errors import MOCK_4001_SCENARIO_NOT_FOUND, ScenarioError
from mock.scenario_engine.models import FixOption, HostState, RecoveryCheck, ScenarioSpec
from mock.scenario_engine.state_store import DEFAULT_DB_PATH, MockStateStore

__all__ = [
    "DEFAULT_BASELINE_RANGES",
    "DEFAULT_DB_PATH",
    "DEFAULT_MOCK_SEED",
    "FixOption",
    "HostState",
    "MOCK_4001_SCENARIO_NOT_FOUND",
    "MockStateStore",
    "RecoveryCheck",
    "ScenarioEngine",
    "ScenarioError",
    "ScenarioSpec",
]
