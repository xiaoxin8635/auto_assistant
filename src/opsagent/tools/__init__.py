"""工具层：查询×3 + 执行×1（P1 最简集），统一 ToolResult 结构与 LLM 工具描述导出。

本层只依赖 httpx 与 config，直接封装 mock 服务 HTTP 契约；LLM 适配不在此层（工作流经 infra/llm）。
"""

from __future__ import annotations

from opsagent.config import Settings, get_settings
from opsagent.tools.base import BaseTool, ServiceClient, ToolRegistry, ToolResult
from opsagent.tools.query_tools import QueryCmdbTool, QueryMetricsTool, SearchLogsTool
from opsagent.tools.recovery import RecoveryCheckParams, RecoveryCheckTool, build_recovery_tool
from opsagent.tools.restart_service import RestartServiceTool


def build_default_registry(settings: Settings | None = None) -> ToolRegistry:
    """按配置装配默认工具集（四个工具各连对应 mock 服务端口）。

    Args:
        settings: 全局配置；None 时取进程单例。

    Returns:
        已注册四个工具的注册表。
    """
    settings = settings or get_settings()
    mock, tool_cfg = settings.mock, settings.tools

    def client_for(port: int) -> ServiceClient:
        """构建指向指定 mock 端口的客户端。

        Args:
            port: mock 服务端口。

        Returns:
            ServiceClient 实例。
        """
        return ServiceClient(
            f"http://{mock.host}:{port}",
            timeout_seconds=tool_cfg.timeout_seconds,
            max_retries=tool_cfg.max_retries,
        )

    registry = ToolRegistry()
    registry.register(QueryMetricsTool(client_for(mock.monitor_port)))
    registry.register(SearchLogsTool(client_for(mock.logs_port)))
    registry.register(QueryCmdbTool(client_for(mock.cmdb_port)))
    registry.register(RestartServiceTool(client_for(mock.monitor_port)))
    return registry


__all__ = [
    "BaseTool",
    "QueryCmdbTool",
    "QueryMetricsTool",
    "RecoveryCheckParams",
    "RecoveryCheckTool",
    "RestartServiceTool",
    "SearchLogsTool",
    "ServiceClient",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
    "build_recovery_tool",
]
