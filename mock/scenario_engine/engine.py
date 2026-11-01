"""场景引擎（跨进程版）：状态经 SQLite 状态库共享，指标经确定性 oracle 计算（ADR-0006）。

设计要点：
- 主机状态机 NORMAL → FAULT → RECOVERED；部分缓解后仍属 FAULT（指标改善但不达标）
- 状态/日志/锚点全部持久化在 MockStateStore，四个服务进程看到同一份事实
- 恢复判定 evaluate_recovery 纯客观规则，不经过 LLM；状态机门控（FAULT 永不判恢复）
- 时钟可注入，指标值确定性（无随机源），测试完全可复现
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from mock.scenario_engine.errors import (
    MOCK_4001_SCENARIO_NOT_FOUND,
    MOCK_4004_STATE_INVALID,
    MOCK_4005_DATA_INVALID,
    MOCK_4006_HOST_NOT_FOUND,
    ScenarioError,
)
from mock.scenario_engine.inventory import load_inventory
from mock.scenario_engine.metrics import (
    BASELINE_WINDOW_SECONDS,
    SAMPLE_INTERVAL_SECONDS,
    MetricOracle,
    default_clock,
)
from mock.scenario_engine.models import (
    FixOption,
    ScenarioSpec,
    parse_window_seconds,
)
from mock.scenario_engine.state_store import MockStateStore

# 未被任何场景覆盖的主机使用的兜底基线区间
DEFAULT_BASELINE_RANGES = {"cpu": "5~15", "memory": "20~40"}
# 指标抖动缺省种子（显式声明，进程间一致）
DEFAULT_MOCK_SEED = 42


class ScenarioEngine:
    """场景引擎（无内部可变态）：场景/资产静态加载自 YAML，动态状态全部经 MockStateStore 读写。

    每个服务进程各持有本类一个实例；注入/修复经任一进程写入后，
    其余进程立即可见（SQLite 共享），满足真 4 进程部署语义。
    """

    def __init__(
        self,
        scenarios_dir: Path,
        inventory_path: Path,
        store: MockStateStore,
        *,
        seed: int = DEFAULT_MOCK_SEED,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """加载场景与资产档案，绑定共享状态库。

        Args:
            scenarios_dir: 场景 YAML 目录（hosts.yaml 会被跳过）。
            inventory_path: 主机资产档案路径。
            store: 共享状态库（SQLite）。
            seed: 指标抖动种子（跨进程必须一致）。
            clock: 时钟函数；缺省真实时间，测试注入假时钟。

        Raises:
            ScenarioError: 场景或资产文件非法（MOCK-4005）。
        """
        self._seed = seed
        self._clock = clock or default_clock
        self._scenarios: dict[str, ScenarioSpec] = {}
        for path in sorted(scenarios_dir.glob("*.yaml")):
            if path.name == "hosts.yaml":
                continue
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                spec = ScenarioSpec.model_validate(raw)
            except Exception as exc:
                raise ScenarioError(
                    MOCK_4005_DATA_INVALID, f"场景文件非法: {path.name}: {exc}"
                ) from exc
            self._scenarios[spec.scenario_id] = spec

        self._inventory = load_inventory(inventory_path)
        self._store = store

    # ---------- 场景与资产 ----------

    def scenario_ids(self) -> list[str]:
        """列出已加载场景 ID。

        Returns:
            排序后的场景 ID 列表。
        """
        return sorted(self._scenarios)

    def get_scenario(self, scenario_id: str) -> ScenarioSpec:
        """取场景定义。

        Args:
            scenario_id: 场景 ID。

        Returns:
            场景规格对象。

        Raises:
            ScenarioError: 场景不存在（MOCK-4001）。
        """
        spec = self._scenarios.get(scenario_id)
        if spec is None:
            raise ScenarioError(
                MOCK_4001_SCENARIO_NOT_FOUND, f"场景不存在: {scenario_id}"
            )
        return spec

    def get_cmdb(self, host_id: str) -> dict[str, Any] | None:
        """查询主机资产档案（查无此主机返回 None，这是 CMDB 的正常语义）。

        Args:
            host_id: 主机 ID。

        Returns:
            资产字典副本；不存在时 None。
        """
        asset = self._inventory.get(host_id)
        return dict(asset) if asset else None

    def state_of(self, host_id: str) -> str:
        """查询主机状态标识（由时代线在当前时刻的最后一个片段决定）。

        Args:
            host_id: 主机 ID。

        Returns:
            normal / fault / recovered。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        self._require_host(host_id)
        now = self._clock()
        current = "normal"
        for epoch in self._epochs(host_id):
            if epoch.since <= now:
                current = epoch.state
        return current

    # ---------- 故障注入与修复 ----------

    def inject_fault(self, scenario_id: str) -> dict[str, Any]:
        """按场景注入故障（仅 NORMAL 状态主机可注入）。

        Args:
            scenario_id: 场景 ID。

        Returns:
            注入结果摘要（场景/主机/时刻）。

        Raises:
            ScenarioError: 场景不存在（MOCK-4001）、主机非 NORMAL（MOCK-4004）
                或主机不存在（MOCK-4006）。
        """
        spec = self.get_scenario(scenario_id)
        host = spec.fault.host
        self._require_host(host)
        state = self.state_of(host)
        if state != "normal":
            raise ScenarioError(
                MOCK_4004_STATE_INVALID,
                f"主机 {host} 当前状态 {state}，仅 NORMAL 可注入",
            )
        now = self._clock()
        self._ensure_baseline(host)
        self._store.append_epoch(host, since=now, state="fault", ranges=spec.fault.metric_effect)
        self._store.set_meta(host, active_scenario=scenario_id, fix_at=None)
        # 注入前先落 pre_inject_logs 条正常心跳（避免"一查即中"）
        for k in range(spec.pre_inject_logs, 0, -1):
            self._store.add_log(
                host,
                spec.fault.service,
                "INFO",
                f"{spec.fault.service} heartbeat normal",
                now - k * SAMPLE_INTERVAL_SECONDS,
            )
        self._store.add_log(host, spec.fault.service, "ERROR", spec.fault.log_signature, now)
        return {
            "scenario_id": scenario_id,
            "host": host,
            "state": "fault",
            "injected_at": now,
        }

    def apply_fix(self, host_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """应用修复动作：与场景 fix_options 匹配后迁移状态（不匹配不产生任何效果）。

        匹配规则：action 同名且 fix_options.params 的每个键值都被调用参数覆盖且相等
        （调用方可携带额外键如 request_id）。

        Args:
            host_id: 主机 ID。
            action: 修复动作名。
            params: 调用参数。

        Returns:
            {matched, effect, state, at, reason} 摘要。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        self._require_host(host_id)
        meta = self._store.get_meta(host_id) or {}
        scenario_id = meta.get("active_scenario")
        state = self.state_of(host_id)
        if scenario_id is None or state != "fault":
            return {
                "matched": False,
                "effect": None,
                "state": state,
                "reason": "host_not_in_fault",
            }
        spec = self._scenarios[scenario_id]
        option = self._match_fix_option(spec, action, params)
        if option is None:
            return {
                "matched": False,
                "effect": None,
                "state": state,
                "reason": "no_matching_fix_option",
            }
        now = self._clock()
        # 恢复锚点：无论缓解或恢复都从"最近一次修复"起算评估窗口，
        # 否则回看窗口混入故障前基线采样，缓解后会被误判恢复
        self._store.set_meta(host_id, active_scenario=scenario_id, fix_at=now)
        if option.effect == "recovered":
            self._store.append_epoch(
                host_id, since=now, state="recovered", ranges=spec.pre_condition
            )
            return {
                "matched": True,
                "effect": "recovered",
                "state": "recovered",
                "at": now,
            }
        # 部分缓解：仍 FAULT，指标切换到缓解区间
        merged_ranges = {**spec.fault.metric_effect, **option.partial_metric_effect}
        self._store.append_epoch(host_id, since=now, state="fault", ranges=merged_ranges)
        return {
            "matched": True,
            "effect": "mitigated",
            "state": "fault",
            "at": now,
        }

    def evaluate_recovery(self, host_id: str) -> dict[str, Any]:
        """客观评估恢复条件（全部条件满足才判恢复；不经过 LLM）。

        指标条件取"修复锚点之后"的窗口均值（锚点保证修复即刻可判定）；
        service_status 特殊指标直接取状态机当前值。

        Args:
            host_id: 主机 ID。

        Returns:
            {recovered, checks: [{metric, op, value, actual, passed}], evaluated_at}。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        self._require_host(host_id)
        now = self._clock()
        meta = self._store.get_meta(host_id) or {}
        anchor = meta.get("fix_at")
        scenario_id = meta.get("active_scenario")
        checks: list[dict[str, Any]] = []
        if scenario_id is None:
            # 无活动场景：以状态机为准（NORMAL/RECOVERED 视为满足）
            recovered = self.state_of(host_id) != "fault"
            return {"recovered": recovered, "checks": checks, "evaluated_at": now}
        spec = self._scenarios[scenario_id]
        oracle = MetricOracle(self._epochs(host_id), self._seed)
        for rule in spec.recovery_check:
            window_seconds = parse_window_seconds(rule.window)
            window_start = now - window_seconds
            if anchor is not None:
                window_start = max(window_start, anchor)
            if rule.metric == "service_status":
                actual: float | str | None = self.get_service_status(host_id)
                passed = actual == rule.value if rule.op == "==" else False
            else:
                mean = oracle.mean_in_window(
                    host_id, rule.metric, window_start, now, anchor_ts=anchor
                )
                if mean is None:
                    actual = None
                    passed = False
                else:
                    actual = round(mean, 2)
                    passed = self._compare(mean, rule.op, float(rule.value))
            checks.append(
                {
                    "metric": rule.metric,
                    "op": rule.op,
                    "value": rule.value,
                    "actual": actual,
                    "window_start": window_start,
                    "passed": passed,
                }
            )
        # 状态门控：状态机是客观事实源——FAULT（含缓解）永不判恢复；
        # 指标检查仅作佐证输出（回看窗口可能混入故障前基线，不能单独定谳）
        recovered = self.state_of(host_id) == "recovered" and all(
            c["passed"] for c in checks
        )
        return {
            "recovered": recovered,
            "checks": checks,
            "evaluated_at": now,
        }

    # ---------- 诊断查询 ----------

    def get_metrics(
        self,
        host_id: str,
        metric: str,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """查询指标时间序列（窗口缺省回看一个基线周期）。

        Args:
            host_id: 主机 ID。
            metric: 指标名（service_status 走 get_service_status）。
            start_ts: 窗口起点；None 用 now-基线窗口。
            end_ts: 窗口终点；None 用 now。

        Returns:
            [{timestamp, value}] 时间升序。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        self._require_host(host_id)
        self._ensure_baseline(host_id)
        now = self._clock()
        start = start_ts if start_ts is not None else now - BASELINE_WINDOW_SECONDS
        end = end_ts if end_ts is not None else now
        anchor = (self._store.get_meta(host_id) or {}).get("fix_at")
        oracle = MetricOracle(self._epochs(host_id), self._seed)
        points = oracle.samples_in_window(host_id, metric, start, end, anchor_ts=anchor)
        return [{"timestamp": p.timestamp, "value": round(p.value, 2)} for p in points]

    def get_service_status(self, host_id: str) -> str:
        """查询服务运行状态（P1 约定：三种状态下进程均存活，过载场景靠指标体现）。

        Args:
            host_id: 主机 ID。

        Returns:
            "running"。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        self._require_host(host_id)
        return "running"

    def search_logs(
        self,
        host_id: str,
        service: str | None = None,
        keyword: str = "",
        lines: int = 20,
    ) -> list[dict[str, Any]]:
        """按主机/服务/关键字检索日志（关键字大小写不敏感，时间倒序）。

        Args:
            host_id: 主机 ID。
            service: 服务过滤；None 不过滤。
            keyword: 正文包含关键字；空串匹配全部。
            lines: 最大返回条数。

        Returns:
            日志字典列表（时间倒序）。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        self._require_host(host_id)
        return self._store.search_logs(host_id, service, keyword, lines)

    def reset(self) -> None:
        """复位：清空状态库（时代线/元数据/日志/工单），全部主机回 NORMAL。"""
        self._store.reset_all()

    # ---------- 内部 ----------

    def _require_host(self, host_id: str) -> None:
        """校验主机存在性。

        Args:
            host_id: 主机 ID。

        Raises:
            ScenarioError: 主机不存在（MOCK-4006）。
        """
        if host_id not in self._inventory:
            raise ScenarioError(MOCK_4006_HOST_NOT_FOUND, f"主机不存在: {host_id}")

    def _ensure_baseline(self, host_id: str) -> None:
        """惰性登记主机基线时代片段（幂等：已在库则跳过，防注入片段被覆盖）。

        Args:
            host_id: 主机 ID。
        """
        if self._store.count_epochs(host_id) == 0:
            self._store.append_epoch(
                host_id, since=float("-inf"), state="normal", ranges=self._baseline_for(host_id)
            )

    def _epochs(self, host_id: str) -> list[Any]:
        """取主机时代线（确保基线已登记）。

        Args:
            host_id: 主机 ID。

        Returns:
            RangeEpoch 列表（since 升序）。
        """
        self._ensure_baseline(host_id)
        return self._store.get_epochs(host_id)

    def _baseline_for(self, host_id: str) -> dict[str, str]:
        """取主机基线区间：优先取命中该主机的场景 pre_condition，否则用兜底。

        Args:
            host_id: 主机 ID。

        Returns:
            指标区间字典（`低~高`）。
        """
        for spec in self._scenarios.values():
            if spec.fault.host == host_id:
                return dict(spec.pre_condition)
        return dict(DEFAULT_BASELINE_RANGES)

    @staticmethod
    def _match_fix_option(
        spec: ScenarioSpec, action: str, params: dict[str, Any]
    ) -> FixOption | None:
        """在场景修复选项中匹配动作与参数。

        Args:
            spec: 场景规格。
            action: 动作名。
            params: 调用参数（须覆盖选项声明的全部键值；允许多余键）。

        Returns:
            命中的 FixOption；无命中返回 None。
        """
        for option in spec.fix_options:
            if option.action != action:
                continue
            if all(
                key in params and params[key] == value
                for key, value in option.params.items()
            ):
                return option
        return None

    @staticmethod
    def _compare(actual: float, op: str, threshold: float) -> bool:
        """按算子比较数值。

        Args:
            actual: 实际值。
            op: 比较算子（< / == / >）。
            threshold: 阈值。

        Returns:
            比较结果。

        Raises:
            ScenarioError: 不支持的算子（MOCK-4005）。
        """
        if op == "<":
            return actual < threshold
        if op == "==":
            return actual == threshold
        if op == ">":
            return actual > threshold
        raise ScenarioError(MOCK_4005_DATA_INVALID, f"不支持的恢复判定算子: {op!r}")


__all__ = ["DEFAULT_BASELINE_RANGES", "DEFAULT_MOCK_SEED", "ScenarioEngine"]
