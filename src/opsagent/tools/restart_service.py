"""执行类工具：restart_service（P1 最简版；服务端幂等与工具层权限拦截 P2 落地，见 DESIGN §5.4 / ADR-0003）。"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from opsagent.tools.base import BaseTool, ServiceClient


class RestartServiceParams(BaseModel):
    """restart_service 参数。

    Attributes:
        host_id: 目标主机 ID。
        service_name: 目标服务名。
        request_id: 幂等键（P1 透传给 mock 记录；服务端去重 P2 实现）。
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str
    service_name: str
    request_id: str = Field(min_length=1)


class RestartServiceTool(BaseTool):
    """重启主机上的指定服务（mock-monitor POST /scenario/fix）。"""

    name: ClassVar[str] = "restart_service"
    description: ClassVar[str] = (
        "重启主机上的指定服务。适用：服务进程失控/僵死（如日志采集进程导致 CPU 过载）"
        "时的标准恢复动作。注意：高危操作，执行前必须经人工确认（P1 阶段直通）。"
    )
    risk: ClassVar[str] = "high"
    params_model: ClassVar[type[BaseModel]] = RestartServiceParams

    def __init__(self, client: ServiceClient) -> None:
        """绑定目标服务客户端。

        Args:
            client: 指向 mock-monitor 的 HTTP 客户端。
        """
        self._client = client

    def _execute(self, params: RestartServiceParams) -> Any:
        """转发 POST /scenario/fix（动作名即工具名；参数原样下传供引擎匹配）。

        Args:
            params: 已校验参数。

        Returns:
            mock-monitor 响应体 {matched, effect, state, at, reason}。
        """
        return self._client.post(
            "/scenario/fix",
            json_body={
                "host_id": params.host_id,
                "action": self.name,
                "params": {
                    "host_id": params.host_id,
                    "service_name": params.service_name,
                    "request_id": params.request_id,
                },
            },
        )


__all__ = ["RestartServiceParams", "RestartServiceTool"]
