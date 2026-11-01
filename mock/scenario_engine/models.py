"""场景数据模型：场景 YAML 的强类型映射与区间/时间窗解析。"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mock.scenario_engine.errors import MOCK_4005_DATA_INVALID, ScenarioError


class HostState(str, enum.Enum):
    """模拟主机状态机的三态（ADR-0002）。

    Attributes:
        NORMAL: 正常运行（指标处于基线区间）。
        FAULT: 故障态（指标异常；部分缓解后仍属本态，仅指标改善）。
        RECOVERED: 已恢复（有效修复动作命中后的终态；P1 不含复发回迁）。
    """

    NORMAL = "normal"
    FAULT = "fault"
    RECOVERED = "recovered"


def parse_metric_range(text: str) -> tuple[float, float]:
    """解析 `低~高` 形式的指标区间字符串。

    Args:
        text: 如 `"92~97"`。

    Returns:
        (low, high) 浮点二元组，保证 low <= high。

    Raises:
        ScenarioError: 格式非法（MOCK-4005）。
    """
    parts = text.split("~")
    if len(parts) != 2:
        raise ScenarioError(
            MOCK_4005_DATA_INVALID, f"指标区间格式非法: {text!r}，应为 '低~高'"
        )
    try:
        low, high = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ScenarioError(
            MOCK_4005_DATA_INVALID, f"指标区间数值非法: {text!r}"
        ) from exc
    return (low, high) if low <= high else (high, low)


def parse_window_seconds(text: str) -> float:
    """解析时间窗字符串为秒数（支持 s/m/h 后缀）。

    Args:
        text: 如 `"30s"` / `"2m"` / `"1h"`。

    Returns:
        秒数（float）。

    Raises:
        ScenarioError: 格式非法（MOCK-4005）。
    """
    suffix_seconds = {"s": 1.0, "m": 60.0, "h": 3600.0}
    if len(text) < 2 or text[-1] not in suffix_seconds:
        raise ScenarioError(MOCK_4005_DATA_INVALID, f"时间窗格式非法: {text!r}，应如 '2m'")
    try:
        return float(text[:-1]) * suffix_seconds[text[-1]]
    except ValueError as exc:
        raise ScenarioError(MOCK_4005_DATA_INVALID, f"时间窗数值非法: {text!r}") from exc


class FaultSpec(BaseModel):
    """故障注入定义。

    Attributes:
        type: 故障类型（如 cpu_overload）。
        host: 目标主机 ID。
        service: 目标服务名。
        metric_effect: 指标名 -> 异常区间（`低~高`）。
        log_signature: 注入的错误日志特征签名（关键字检索命中的靶子）。
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    host: str
    service: str
    metric_effect: dict[str, str]
    log_signature: str


class FixOption(BaseModel):
    """修复动作定义。

    Attributes:
        action: 修复动作名（须与工具层动作对齐，如 restart_service）。
        params: 动作参数（工具调用参数须与其匹配才生效）。
        effect: 生效类型——recovered=迁到恢复态；mitigated=部分缓解（仍 FAULT）。
        partial_metric_effect: mitigated 时的指标改善区间（`低~高`）。
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    params: dict[str, Any]
    effect: Literal["recovered", "mitigated"]
    partial_metric_effect: dict[str, str] = Field(default_factory=dict)

    @field_validator("partial_metric_effect")
    @classmethod
    def _validate_partial(cls, v: dict[str, str]) -> dict[str, str]:
        """校验缓解区间字符串可解析，并禁止 recovered 声明缓解区间。

        Args:
            v: 指标区间字典。

        Returns:
            原值（校验通过）。

        Raises:
            ValueError: 区间不可解析或 recovered 携带缓解区间。
        """
        for text in v.values():
            parse_metric_range(text)
        return v


class RecoveryCheck(BaseModel):
    """恢复条件（客观判定，不经过 LLM）。

    Attributes:
        metric: 指标名（service_status 为特殊指标，取自状态机）。
        op: 比较算子（< / == / >）。
        value: 阈值（数值或状态字符串）。
        window: 聚合时间窗（如 `2m`）。
    """

    model_config = ConfigDict(extra="forbid")

    metric: str
    op: Literal["<", "==", ">"]
    value: float | str
    window: str = "2m"


class ScenarioSpec(BaseModel):
    """场景定义（SC-CPU-001 等 YAML 的强类型映射）。

    Attributes:
        scenario_id: 场景 ID（SC-<类型>-<序号>）。
        title: 场景标题。
        difficulty: 难度分类（normal/abnormal/edge/adversarial）。
        fault: 故障注入定义。
        pre_condition: 注入前正常基线区间（供修复后对照）。
        pre_inject_logs: 注入前先产生的正常日志条数（避免"一查即中"）。
        fix_options: 修复动作列表（含正确修复与部分缓解）。
        recovery_check: 恢复条件列表（全部满足才算恢复）。
        knowledge_hints: 知识库命中提示（P2 RAG 语料对齐用）。
        token_budget: 本场景 token 预算。
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    title: str
    difficulty: Literal["normal", "abnormal", "edge", "adversarial"] = "normal"
    fault: FaultSpec
    pre_condition: dict[str, str]
    pre_inject_logs: int = Field(default=2, ge=0)
    fix_options: list[FixOption]
    recovery_check: list[RecoveryCheck]
    knowledge_hints: list[str] = Field(default_factory=list)
    token_budget: int = Field(default=8000, gt=0)

    @field_validator("pre_condition")
    @classmethod
    def _validate_pre_condition(cls, v: dict[str, str]) -> dict[str, str]:
        """校验基线区间字符串可解析。

        Args:
            v: 基线区间字典。

        Returns:
            原值。

        Raises:
            ValueError: 存在不可解析的区间。
        """
        for text in v.values():
            parse_metric_range(text)
        return v

    @field_validator("fix_options")
    @classmethod
    def _validate_fix_options(cls, v: list[FixOption]) -> list[FixOption]:
        """场景必须恰好包含一个 recovered 修复（保证可解）。

        Args:
            v: 修复动作列表。

        Returns:
            原列表。

        Raises:
            ValueError: recovered 修复数量不为 1。
        """
        recovered = sum(1 for option in v if option.effect == "recovered")
        if recovered != 1:
            raise ValueError(f"场景必须且只能有一个 recovered 修复，实际 {recovered} 个")
        return v
