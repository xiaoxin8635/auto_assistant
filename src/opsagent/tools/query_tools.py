"""查询类工具（DESIGN §5.4）：query_metrics / search_logs / query_cmdb，只读天然幂等，风险低。"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from opsagent.tools.base import BaseTool, ServiceClient


class QueryMetricsParams(BaseModel):
    """query_metrics 参数。

    Attributes:
        host_id: 主机 ID。
        metric: 指标名（cpu/memory 等）。
        start_ts: 窗口起点（epoch 秒；缺省回看一个基线周期）。
        end_ts: 窗口终点（epoch 秒；缺省 now）。
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str
    metric: str
    start_ts: float | None = None
    end_ts: float | None = None


class QueryMetricsTool(BaseTool):
    """指标时间序列查询（mock-monitor GET /metrics）。"""

    name: ClassVar[str] = "query_metrics"
    description: ClassVar[str] = (
        "查询指定主机的指标时间序列（cpu/memory 等，返回 [{timestamp, value}]）。"
        "适用：判断资源是否过载、定位异常起始时间、验证修复后指标是否回落。"
    )
    risk: ClassVar[str] = "low"
    params_model: ClassVar[type[BaseModel]] = QueryMetricsParams

    def __init__(self, client: ServiceClient) -> None:
        """绑定目标服务客户端。

        Args:
            client: 指向 mock-monitor 的 HTTP 客户端。
        """
        self._client = client

    def _execute(self, params: QueryMetricsParams) -> Any:
        """转发 GET /metrics（None 参数不下发，走服务端缺省窗口）。

        Args:
            params: 已校验参数。

        Returns:
            mock-monitor 响应体 {host_id, metric, points}。
        """
        query = {key: value for key, value in params.model_dump().items() if value is not None}
        return self._client.get("/metrics", params=query)


class SearchLogsParams(BaseModel):
    """search_logs 参数。

    Attributes:
        host_id: 主机 ID。
        service: 服务过滤；None 不过滤。
        keyword: 正文关键字（大小写不敏感）；空串匹配全部。
        lines: 最大返回条数（1~200）。
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str
    service: str | None = None
    keyword: str = ""
    lines: int = Field(default=20, ge=1, le=200)


class SearchLogsTool(BaseTool):
    """日志检索（mock-logs GET /search）。"""

    name: ClassVar[str] = "search_logs"
    description: ClassVar[str] = (
        "按主机/服务/关键字检索系统日志（时间倒序，带级别）。"
        "适用：寻找错误特征签名、确认故障时间线、佐证根因假设。"
    )
    risk: ClassVar[str] = "low"
    params_model: ClassVar[type[BaseModel]] = SearchLogsParams

    def __init__(self, client: ServiceClient) -> None:
        """绑定目标服务客户端。

        Args:
            client: 指向 mock-logs 的 HTTP 客户端。
        """
        self._client = client

    def _execute(self, params: SearchLogsParams) -> Any:
        """转发 GET /search（service 为 None 不下发）。

        Args:
            params: 已校验参数。

        Returns:
            mock-logs 响应体 {host_id, entries}。
        """
        query = {key: value for key, value in params.model_dump().items() if value is not None}
        return self._client.get("/search", params=query)


class QueryCmdbParams(BaseModel):
    """query_cmdb 参数。

    Attributes:
        host_id: 主机 ID。
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str


class QueryCmdbTool(BaseTool):
    """资产档案查询（mock-cmdb GET /host）。"""

    name: ClassVar[str] = "query_cmdb"
    description: ClassVar[str] = (
        "查询主机资产档案（IP/OS/所属服务/负责人）。"
        "适用：诊断前了解主机背景、判断服务归属与影响面。"
    )
    risk: ClassVar[str] = "low"
    params_model: ClassVar[type[BaseModel]] = QueryCmdbParams

    def __init__(self, client: ServiceClient) -> None:
        """绑定目标服务客户端。

        Args:
            client: 指向 mock-cmdb 的 HTTP 客户端。
        """
        self._client = client

    def _execute(self, params: QueryCmdbParams) -> Any:
        """转发 GET /host。

        Args:
            params: 已校验参数。

        Returns:
            资产字典；查无此主机时客户端透传 MOCK-4006。
        """
        return self._client.get("/host", params=params.model_dump())


__all__ = [
    "QueryCmdbParams",
    "QueryCmdbTool",
    "QueryMetricsParams",
    "QueryMetricsTool",
    "SearchLogsParams",
    "SearchLogsTool",
]
