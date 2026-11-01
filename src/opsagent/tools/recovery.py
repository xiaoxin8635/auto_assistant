"""恢复判定工具（check_recovery）：查询 mock-monitor /recovery_check 的客观恢复结论。

工作流内部专用——不注册进默认 LLM 工具集（build_default_registry），由 validate 节点直接持有。
生产语义对应监控系统 SLO / 告警恢复判定 API。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from opsagent.tools.base import BaseTool, ServiceClient


class RecoveryCheckParams(BaseModel):
    """恢复判定入参。

    Attributes:
        host_id: 主机 ID。
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str


class RecoveryCheckTool(BaseTool):
    """恢复判定工具：返回 {recovered, checks, evaluated_at} 客观判定结果。"""

    name: str = "check_recovery"
    description: str = (
        "检查指定主机的告警恢复条件是否达成（客观判定，非模型自评）。"
        "在执行修复动作之后调用，用于确认故障是否已恢复。"
    )
    risk: str = "low"
    params_model: type[RecoveryCheckParams] = RecoveryCheckParams

    def __init__(self, client: ServiceClient) -> None:
        """绑定恢复判定服务客户端。

        Args:
            client: 指向 mock-monitor 的 ServiceClient。
        """
        self._client = client

    def _execute(self, params: RecoveryCheckParams) -> dict:
        """GET /recovery_check 获取恢复判定。

        Args:
            params: 已校验入参。

        Returns:
            判定结果 dict（recovered / checks / evaluated_at）。
        """
        payload = self._client.get("/recovery_check", params={"host_id": params.host_id})
        return dict(payload)


def build_recovery_tool(settings: object | None = None) -> RecoveryCheckTool:
    """按配置装配恢复判定工具（工作流专用，不进默认注册表）。

    Args:
        settings: 全局配置；None 时取进程单例。

    Returns:
        RecoveryCheckTool 实例。
    """
    from opsagent.config import Settings, get_settings

    resolved: Settings = settings or get_settings()  # type: ignore[assignment]
    mock, tool_cfg = resolved.mock, resolved.tools
    client = ServiceClient(
        f"http://{mock.host}:{mock.monitor_port}",
        timeout_seconds=tool_cfg.timeout_seconds,
        max_retries=tool_cfg.max_retries,
    )
    return RecoveryCheckTool(client)


__all__ = ["RecoveryCheckParams", "RecoveryCheckTool", "build_recovery_tool"]
