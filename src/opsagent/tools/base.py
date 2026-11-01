"""工具层基座：统一 ToolResult 结构、HTTP 客户端（超时+指数退避重试）、工具基类与注册表。

容错约定（DESIGN §5.4）：模拟服务超时 5s，传输失败与 5xx/429 退避重试 ≤2 次；
4xx 业务错误不重试（重试无意义）；最终失败以结构化错误返回，由工作流决定降级或转人工。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ValidationError

from opsagent.errors import (
    TOOL_1001_TOOL_CALL_FAILED,
    TOOL_1002_TOOL_PARAM_INVALID,
    TOOL_1003_TOOL_NOT_FOUND,
    OpsAgentError,
)

# 视为可重试的 HTTP 状态码（连接失败/超时走 httpx.HTTPError 分支）
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ToolResult(BaseModel):
    """工具统一返回结构（工作流消费证据、审计落库都以此为准）。

    Attributes:
        success: 业务是否成功（传输与业务错误均为 False；matched=False 属业务结果，仍为 True）。
        data: 成功时的载荷（mock 服务响应体）。
        error_code: 失败时的错误码（TOOL-1xxx 或透传的 MOCK-4xxx 等）。
        error_message: 失败时的人类可读描述。
        elapsed_ms: 工具执行耗时（毫秒，含参数校验与重试）。
    """

    success: bool
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    elapsed_ms: float = 0.0

    @classmethod
    def ok(cls, data: Any, elapsed_ms: float) -> ToolResult:
        """构造成功结果。

        Args:
            data: mock 服务响应体。
            elapsed_ms: 耗时。

        Returns:
            success=True 的结果。
        """
        return cls(success=True, data=data, elapsed_ms=elapsed_ms)

    @classmethod
    def fail(cls, code: str, message: str, elapsed_ms: float) -> ToolResult:
        """构造失败结果。

        Args:
            code: 错误码。
            message: 描述。
            elapsed_ms: 耗时。

        Returns:
            success=False 的结果。
        """
        return cls(success=False, error_code=code, error_message=message, elapsed_ms=elapsed_ms)


class ServiceClient:
    """mock 服务 HTTP 客户端：短连接 + 超时 + 指数退避重试。

    Attributes:
        _client: httpx 同步客户端（transport 可注入，测试用 MockTransport）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """绑定目标服务。

        Args:
            base_url: 服务根地址（如 http://127.0.0.1:8101）。
            timeout_seconds: 单次请求超时。
            max_retries: 可重试错误的最大重试次数（总请求 = 1 + max_retries）。
            backoff_base: 退避基数（第 k 次重试等待 backoff_base * 2^k 秒；测试调小）。
            transport: httpx 传输层（None 走真实网络）。
        """
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds, transport=transport)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET 请求。

        Args:
            path: 路径。
            params: 查询参数。

        Returns:
            响应 JSON。

        Raises:
            OpsAgentError: 调用失败（TOOL-1001 或透传上游错误码）。
        """
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: dict[str, Any] | None = None) -> Any:
        """POST 请求。

        Args:
            path: 路径。
            json_body: 请求体。

        Returns:
            响应 JSON。

        Raises:
            OpsAgentError: 调用失败。
        """
        return self._request("POST", path, json=json_body)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """带重试的请求主循环。

        Args:
            method: HTTP 方法。
            path: 路径。
            **kwargs: httpx 请求参数。

        Returns:
            响应 JSON。

        Raises:
            OpsAgentError: 重试耗尽或不可重试错误。
        """
        last_error: OpsAgentError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_error = OpsAgentError(
                    TOOL_1001_TOOL_CALL_FAILED,
                    f"模拟服务请求失败: {exc}",
                    {"path": path, "attempt": attempt},
                )
            else:
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise OpsAgentError(
                            TOOL_1001_TOOL_CALL_FAILED,
                            f"模拟服务响应非 JSON: {path}",
                            {"path": path},
                        ) from exc
                error = self._error_from_response(response, path)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    last_error = error
                else:
                    raise error
            if attempt < self._max_retries:
                time.sleep(self._backoff_base * (2**attempt))
        assert last_error is not None  # max_retries >= 0 时循环至少赋值一次
        raise last_error

    @staticmethod
    def _error_from_response(response: httpx.Response, path: str) -> OpsAgentError:
        """把非 2xx 响应映射为 OpsAgentError（mock 错误体存在则透传其错误码）。

        Args:
            response: 非成功响应。
            path: 请求路径。

        Returns:
            业务异常。
        """
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("code"), str):
            return OpsAgentError(
                body["code"],
                str(body.get("message", "模拟服务返回错误")),
                {"status": response.status_code, "path": path, "detail": body.get("detail")},
            )
        return OpsAgentError(
            TOOL_1001_TOOL_CALL_FAILED,
            f"模拟服务返回 {response.status_code}: {path}",
            {"status": response.status_code, "path": path},
        )


class BaseTool(ABC):
    """工具基类：参数校验 + 计时 + 异常映射，子类只实现 _execute。

    ClassVars:
        name: 工具名（LLM function-calling 的函数名，与 DESIGN §5.4 对齐）。
        description: 工具描述（功能边界 + 适用场景，供模型选择）。
        risk: 风险分级（low/medium/high；P1 直通，P2 权限与审核依赖此字段）。
        params_model: 参数 pydantic 模型（校验 + JSON Schema 导出双重用途）。
    """

    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[Literal["low", "medium", "high"]]
    params_model: ClassVar[type[BaseModel]]

    def run(self, raw_params: dict[str, Any]) -> ToolResult:
        """执行工具（统一入口）。

        Args:
            raw_params: LLM 产出的原始参数字典。

        Returns:
            ToolResult（永不抛业务异常；编程期错误仍直接暴露）。
        """
        started = time.perf_counter()

        def elapsed() -> float:
            """计算自开始起耗时（毫秒）。

            Returns:
                毫秒数（保留 3 位小数）。
            """
            return round((time.perf_counter() - started) * 1000, 3)

        try:
            params = self.params_model.model_validate(raw_params)
        except ValidationError as exc:
            return ToolResult.fail(TOOL_1002_TOOL_PARAM_INVALID, _format_validation_error(exc), elapsed())
        try:
            data = self._execute(params)
        except OpsAgentError as exc:
            return ToolResult.fail(exc.code, exc.message, elapsed())
        return ToolResult.ok(data, elapsed())

    @abstractmethod
    def _execute(self, params: Any) -> Any:
        """执行业务动作（子类实现；只许抛 OpsAgentError）。

        Args:
            params: 已校验的参数模型。

        Returns:
            mock 服务响应体。
        """

    def to_openai_schema(self) -> dict[str, Any]:
        """导出 OpenAI function-calling 工具描述。

        Returns:
            {type: function, function: {name, description, parameters}}。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_model.model_json_schema(),
            },
        }


class ToolRegistry:
    """工具注册表：按名取工具 + 导出 LLM 工具描述集合。"""

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具（重名视为编程期错误，直接拒绝）。

        Args:
            tool: 工具实例。

        Raises:
            ValueError: 工具名重复。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        """按名取工具。

        Args:
            name: 工具名。

        Returns:
            工具实例。

        Raises:
            OpsAgentError: 未注册（TOOL-1003）。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise OpsAgentError(TOOL_1003_TOOL_NOT_FOUND, f"工具未注册: {name}")
        return tool

    def names(self) -> list[str]:
        """列出已注册工具名（排序）。

        Returns:
            工具名列表。
        """
        return sorted(self._tools)

    def openai_schemas(self) -> list[dict[str, Any]]:
        """导出全部工具的 OpenAI function-calling 描述（排序稳定）。

        Returns:
            工具描述列表。
        """
        return [self._tools[name].to_openai_schema() for name in sorted(self._tools)]


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 校验错误压缩为单行描述。

    Args:
        exc: 校验异常。

    Returns:
        `字段: 消息; 字段: 消息` 形式的字符串。
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )


__all__ = [
    "BaseTool",
    "RETRYABLE_STATUS_CODES",
    "ServiceClient",
    "ToolRegistry",
    "ToolResult",
]
