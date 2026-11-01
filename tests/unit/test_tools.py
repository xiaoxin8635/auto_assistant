"""工具层测试：httpx.MockTransport 模拟 mock 服务（不起端口），覆盖参数校验、重试容错、错误映射与注册表。"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from opsagent.config import Settings
from opsagent.errors import (
    TOOL_1001_TOOL_CALL_FAILED,
    TOOL_1002_TOOL_PARAM_INVALID,
    TOOL_1003_TOOL_NOT_FOUND,
    OpsAgentError,
)
from opsagent.tools import build_default_registry
from opsagent.tools.base import ServiceClient, ToolRegistry
from opsagent.tools.query_tools import QueryCmdbTool, QueryMetricsTool, SearchLogsTool
from opsagent.tools.restart_service import RestartServiceTool

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, **kwargs: Any) -> ServiceClient:
    """构建走 MockTransport 的客户端（退避基数调小，测试提速）。

    Args:
        handler: 模拟响应函数。
        **kwargs: ServiceClient 其余参数。

    Returns:
        ServiceClient 实例。
    """
    kwargs.setdefault("backoff_base", 0.001)
    return ServiceClient("http://mock.test", transport=httpx.MockTransport(handler), **kwargs)


def test_query_metrics_success() -> None:
    """query_metrics：正常返回指标点。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """校验请求形状并返回指标数据。

        Args:
            request: 待处理请求。

        Returns:
            指标响应。
        """
        assert request.url.path == "/metrics"
        assert request.url.params["host_id"] == "host-web-01"
        assert request.url.params["metric"] == "cpu"
        return httpx.Response(
            200,
            json={"host_id": "host-web-01", "metric": "cpu", "points": [{"timestamp": 1.0, "value": 95.1}]},
        )

    result = QueryMetricsTool(make_client(handler)).run({"host_id": "host-web-01", "metric": "cpu"})
    assert result.success is True
    assert result.data["points"][0]["value"] == 95.1
    assert result.elapsed_ms >= 0


def test_invalid_params_fail_fast() -> None:
    """参数缺字段：TOOL-1002，且不发起任何 HTTP 请求。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """不应被调用的兜底。

        Args:
            request: 待处理请求。

        Raises:
            AssertionError: 参数校验失败后不应发起请求。
        """
        raise AssertionError("参数校验失败后不应发起请求")

    result = QueryMetricsTool(make_client(handler)).run({"metric": "cpu"})
    assert result.success is False
    assert result.error_code == TOOL_1002_TOOL_PARAM_INVALID
    assert "host_id" in (result.error_message or "")


def test_mock_business_error_propagates_code() -> None:
    """mock 4xx 错误体：透传 MOCK-4006，且 4xx 不重试。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回业务错误。

        Args:
            request: 待处理请求。

        Returns:
            404 错误响应。
        """
        calls.append(request)
        return httpx.Response(404, json={"code": "MOCK-4006", "message": "主机不存在: host-ghost", "detail": {}})

    result = QueryCmdbTool(make_client(handler)).run({"host_id": "host-ghost"})
    assert result.success is False
    assert result.error_code == "MOCK-4006"
    assert result.error_message == "主机不存在: host-ghost"
    assert len(calls) == 1


def test_server_error_without_code_maps_to_tool_code() -> None:
    """上游 500 且无错误体：映射 TOOL-1001。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回无错误体的 500。

        Args:
            request: 待处理请求。

        Returns:
            500 响应。
        """
        return httpx.Response(500, text="boom")

    result = QueryMetricsTool(make_client(handler, max_retries=0)).run({"host_id": "h", "metric": "cpu"})
    assert result.success is False
    assert result.error_code == TOOL_1001_TOOL_CALL_FAILED


def test_retry_on_5xx_then_success() -> None:
    """5xx 可重试：第一次 503、第二次 200 → 最终成功。"""
    responses = iter([
        httpx.Response(503),
        httpx.Response(200, json={"host_id": "h", "metric": "cpu", "points": []}),
    ])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """按序返回响应。

        Args:
            request: 待处理请求。

        Returns:
            队列中的下一个响应。
        """
        calls.append(request)
        return next(responses)

    result = QueryMetricsTool(make_client(handler)).run({"host_id": "h", "metric": "cpu"})
    assert result.success is True
    assert len(calls) == 2


def test_transport_error_exhausts_retries() -> None:
    """连接失败：重试耗尽后返回 TOOL-1001（总请求 = 1 + 默认 max_retries）。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """模拟连接拒绝。

        Args:
            request: 待处理请求。

        Raises:
            httpx.ConnectError: 每次都失败。
        """
        calls.append(request)
        raise httpx.ConnectError("connection refused")

    result = SearchLogsTool(make_client(handler)).run({"host_id": "h"})
    assert result.success is False
    assert result.error_code == TOOL_1001_TOOL_CALL_FAILED
    assert len(calls) == 3


def test_search_logs_defaults_forwarded() -> None:
    """search_logs：缺省 keyword/lines 正确转发。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """校验缺省查询参数。

        Args:
            request: 待处理请求。

        Returns:
            空日志响应。
        """
        assert request.url.params["keyword"] == ""
        assert request.url.params["lines"] == "20"
        return httpx.Response(200, json={"host_id": "h", "entries": []})

    result = SearchLogsTool(make_client(handler)).run({"host_id": "h"})
    assert result.success is True


def test_restart_service_request_shape() -> None:
    """restart_service：POST /scenario/fix，请求体结构与 mock 契约对齐。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获请求并返回恢复结果。

        Args:
            request: 待处理请求。

        Returns:
            修复结果响应。
        """
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"matched": True, "effect": "recovered", "state": "recovered", "at": 1.0})

    params = {"host_id": "host-web-01", "service_name": "log-agent", "request_id": "req-1"}
    result = RestartServiceTool(make_client(handler)).run(params)
    assert result.success is True
    assert result.data["effect"] == "recovered"
    assert captured["path"] == "/scenario/fix"
    body = captured["body"]
    assert body["action"] == "restart_service"
    assert body["host_id"] == "host-web-01"
    assert body["params"] == params  # 三键原样下传（前两键供引擎匹配，request_id 供幂等审计）


def test_registry_get_missing_raises() -> None:
    """注册表：未注册工具抛 TOOL-1003。"""
    registry = ToolRegistry()
    with pytest.raises(OpsAgentError) as exc_info:
        registry.get("nope")
    assert exc_info.value.code == TOOL_1003_TOOL_NOT_FOUND


def test_registry_duplicate_rejected() -> None:
    """重名注册视为编程期错误，直接拒绝。"""
    registry = ToolRegistry()
    tool = QueryCmdbTool(make_client(lambda request: httpx.Response(200, json={})))
    registry.register(tool)
    with pytest.raises(ValueError):
        registry.register(tool)


def test_build_default_registry_schemas() -> None:
    """默认注册表：四工具齐备，OpenAI schema 结构合法且风险分级正确。"""
    registry = build_default_registry(Settings())
    assert registry.names() == ["query_cmdb", "query_metrics", "restart_service", "search_logs"]
    schemas = registry.openai_schemas()
    by_name = {schema["function"]["name"] for schema in schemas}
    assert by_name == set(registry.names())
    for schema in schemas:
        assert schema["type"] == "function"
        assert "parameters" in schema["function"]
    assert registry.get("restart_service").risk == "high"
    assert registry.get("query_metrics").risk == "low"
