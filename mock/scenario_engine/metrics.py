"""指标确定性 oracle（ADR-0006）：采样值是纯函数，跨进程计算同一窗口结果逐位一致。

value(seed, host, metric, tick) = low + (high - low) * frac(hash(seed|host|metric|tick))，
数值区间由时代时间线按时刻推导。不保存采样序列——四个服务进程共享状态库时
无需同步采样数据，测试也天然确定（无随机源）。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

# 采样节奏与历史窗口（集中常量，避免魔法值）
SAMPLE_INTERVAL_SECONDS = 10.0  # 采样间隔
BASELINE_WINDOW_SECONDS = 300.0  # 默认回看窗口（一个基线周期）


@dataclass(frozen=True)
class Sample:
    """单条指标采样点。

    Attributes:
        timestamp: 采样时刻（epoch 秒）。
        value: 指标值。
    """

    timestamp: float
    value: float


@dataclass(frozen=True)
class RangeEpoch:
    """区间生效时间线片段：自 `since` 时刻起 state 决定各指标取值区间。

    Attributes:
        since: 生效起始时刻（epoch 秒；首片段用 -inf 表示自起始即生效）。
        state: 该时刻的状态标识（normal/fault/recovered）。
        ranges: 指标名 -> `低~高` 区间字符串。
    """

    since: float
    state: str
    ranges: dict[str, str]


def deterministic_frac(seed: int, host: str, metric: str, ts: float) -> float:
    """把 (种子, 主机, 指标, 时刻) 确定性映射到 [0, 1) 抖动系数。

    同一输入任何进程任意时刻计算结果相同（md5 整数化取高 64 位），
    这是跨进程指标一致性的根基（ADR-0006）。

    Args:
        seed: 引擎种子。
        host: 主机 ID。
        metric: 指标名。
        ts: 采样时刻。

    Returns:
        [0, 1) 浮点数。
    """
    digest = hashlib.md5(f"{seed}|{host}|{metric}|{ts:.0f}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class MetricOracle:
    """给定时代时间线的纯函数指标视图（无内部可变态）。

    每次状态迁移（注入/修复）产生新的 RangeEpoch；查询时按时刻定位区间
    并用确定性抖动生成采样值。
    """

    def __init__(self, epochs: list[RangeEpoch], seed: int) -> None:
        """构建 oracle 视图。

        Args:
            host_epochs: 该主机的区间时代片段（按 since 升序）。
            seed: 抖动种子。
        """
        self._epochs = sorted(epochs, key=lambda e: e.since)
        self._seed = seed

    def _range_at(self, metric: str, ts: float) -> tuple[float, float]:
        """取某历史时刻指标所处的数值区间。

        Args:
            metric: 指标名。
            ts: 历史时刻。

        Returns:
            (low, high)；该时刻无定义时回退时间线上最后一次定义。
        """
        known: str | None = None
        for epoch in self._epochs:
            if epoch.since <= ts and metric in epoch.ranges:
                known = epoch.ranges[metric]
        if known is None:
            for epoch in reversed(self._epochs):
                if metric in epoch.ranges:
                    known = epoch.ranges[metric]
                    break
        low, high = (float(part) for part in known.split("~"))
        return (low, high) if low <= high else (high, low)

    def _value_at(self, host: str, metric: str, ts: float) -> Sample:
        """生成某时刻的采样点（区间 × 确定性抖动）。

        Args:
            host: 主机 ID。
            metric: 指标名。
            ts: 采样时刻。

        Returns:
            采样点。
        """
        low, high = self._range_at(metric, ts)
        frac = deterministic_frac(self._seed, host, metric, ts)
        return Sample(timestamp=ts, value=low + (high - low) * frac)

    def samples_in_window(
        self,
        host: str,
        metric: str,
        start_ts: float,
        end_ts: float,
        anchor_ts: float | None = None,
    ) -> list[Sample]:
        """返回 [start_ts, end_ts] 内的采样点（时间升序）。

        采样点落在固定刻度网格上（间隔 SAMPLE_INTERVAL_SECONDS）；
        恢复锚点若不落在网格上则额外插入一个锚点采样（修复即刻可判定，
        且锚点值与网格值同源同区间——纯函数语义下无"重画"概念）。

        Args:
            host: 主机 ID。
            metric: 指标名。
            start_ts: 窗口起点。
            end_ts: 窗口终点。
            anchor_ts: 恢复锚点时刻；提供且落在窗口内时保证该时刻有采样点。

        Returns:
            窗口内采样点（时间升序）。
        """
        first_tick = math.ceil(start_ts / SAMPLE_INTERVAL_SECONDS) * SAMPLE_INTERVAL_SECONDS
        samples = [
            self._value_at(host, metric, tick)
            for tick in _tick_range(first_tick, end_ts)
        ]
        if (
            anchor_ts is not None
            and start_ts <= anchor_ts <= end_ts
            and not any(math.isclose(s.timestamp, anchor_ts) for s in samples)
        ):
            samples.append(self._value_at(host, metric, anchor_ts))
            samples.sort(key=lambda s: s.timestamp)
        return samples

    def mean_in_window(
        self,
        host: str,
        metric: str,
        start_ts: float,
        end_ts: float,
        anchor_ts: float | None = None,
    ) -> float | None:
        """窗口内采样均值；窗口内无采样点时返回 None。

        Args:
            host: 主机 ID。
            metric: 指标名。
            start_ts: 窗口起点。
            end_ts: 窗口终点。
            anchor_ts: 恢复锚点时刻（可选）。

        Returns:
            均值；无采样点时 None。
        """
        points = self.samples_in_window(host, metric, start_ts, end_ts, anchor_ts)
        if not points:
            return None
        return sum(p.value for p in points) / len(points)


def _tick_range(first_tick: float, end_ts: float) -> list[float]:
    """生成 [first_tick, end_ts] 的采样刻度序列。

    Args:
        first_tick: 首个刻度（已对齐网格）。
        end_ts: 截止时刻（含）。

    Returns:
        刻度列表（升序）。
    """
    ticks: list[float] = []
    tick = first_tick
    while tick <= end_ts:
        ticks.append(tick)
        tick += SAMPLE_INTERVAL_SECONDS
    return ticks


def default_clock() -> float:
    """默认时钟（真实时间），测试可注入替代。

    Returns:
        当前 epoch 秒。
    """
    return time.time()


__all__ = [
    "BASELINE_WINDOW_SECONDS",
    "SAMPLE_INTERVAL_SECONDS",
    "MetricOracle",
    "RangeEpoch",
    "Sample",
    "default_clock",
    "deterministic_frac",
]
