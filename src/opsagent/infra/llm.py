"""LLM 适配器：统一 chat() 接口（fast/deep 双 tier 路由），隔离 openai SDK（ADR-0005）。

接口契约：业务代码零 `import openai`；超时/重试/温度/token 统计全部收敛在本模块。
实现：OpenAICompatLLMAdapter（DeepSeek/通义，生产）、FakeLLMAdapter（测试脚本化替身）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from opsagent.config import ModelConfig
from opsagent.errors import (
    INF_8002_LLM_CALL_FAILED,
    INF_8003_LLM_CONFIG_INVALID,
    OpsAgentError,
)
from opsagent.log import get_logger

# tier 语义：fast=提取/摘要类轻量调用；deep=诊断推理类调用
Tier = Literal["fast", "deep"]

_logger = get_logger("infra.llm")


class TokenUsage(BaseModel):
    """单次调用的 token 用量（成本统计与预算熔断的数据源）。

    Attributes:
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        total_tokens: 总 token 数。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ToolCallOut(BaseModel):
    """模型输出的工具调用请求（id/name/arguments 从上游原样透传）。

    Attributes:
        id: 工具调用 ID（回传结果时使用）。
        name: 工具名。
        arguments_json: 参数 JSON 字符串（原样保留，由调用方解析校验）。
    """

    id: str
    name: str
    arguments_json: str


class LLMResponse(BaseModel):
    """chat() 的统一返回结构。

    Attributes:
        content: 文本回复内容（工具调用轮次可能为 None）。
        tool_calls: 模型请求的工具调用列表。
        usage: token 用量。
        model: 实际使用的模型名（审计与评测快照用）。
    """

    content: str | None = None
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""


class LLMAdapter(Protocol):
    """LLM 适配器接口（生产/测试双实现，结构化子类型即满足）。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: Tier = "deep",
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """发送对话补全请求。

        Args:
            messages: OpenAI 兼容消息列表。
            tier: 模型档位（fast/deep）。
            tools: function-calling 工具 schema 列表；None 表示纯文本调用。
            temperature: 覆盖默认采样温度；None 用 ModelConfig 默认值。

        Returns:
            统一 LLMResponse。

        Raises:
            OpsAgentError: 调用失败（INF-8002）或配置缺失（INF-8003）。
        """
        ...


class OpenAICompatLLMAdapter:
    """OpenAI 兼容协议适配器（DeepSeek / 通义千问按 base_url + api_key 切换）。"""

    def __init__(self, config: ModelConfig) -> None:
        """初始化适配器（客户端按 tier 惰性创建并复用）。

        Args:
            config: LLM 接入配置。
        """
        self._config = config
        self._clients: dict[Tier, OpenAI] = {}

    def _client_for(self, tier: Tier) -> OpenAI:
        """获取指定 tier 的 SDK 客户端（同 tier 复用）。

        Args:
            tier: 模型档位。

        Returns:
            openai SDK 客户端实例。

        Raises:
            OpsAgentError: api_key 未配置（错误码 INF-8003）。
        """
        if tier not in self._clients:
            api_key = self._config.api_key.get_secret_value()
            if not api_key:
                raise OpsAgentError(
                    INF_8003_LLM_CONFIG_INVALID,
                    "LLM API key 未配置：请在 config/model.yaml 或环境变量中填写",
                    {"provider": self._config.provider, "tier": tier},
                )
            self._clients[tier] = OpenAI(
                api_key=api_key,
                base_url=self._config.base_url,
                timeout=self._config.timeout_seconds,
                max_retries=self._config.max_retries,
            )
        return self._clients[tier]

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: Tier = "deep",
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """发送对话补全请求并归一化响应。

        Args:
            messages: OpenAI 兼容消息列表。
            tier: 模型档位（fast/deep）。
            tools: function-calling 工具 schema 列表；None 表示纯文本调用。
            temperature: 覆盖默认采样温度。

        Returns:
            统一 LLMResponse（含 usage 与实际模型名）。

        Raises:
            OpsAgentError: SDK 调用失败（INF-8002）、空响应（INF-8002）或 key 缺失（INF-8003）。
        """
        model_name = self._config.model_fast if tier == "fast" else self._config.model_deep
        request_kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": self._config.temperature if temperature is None else temperature,
        }
        if tools:
            request_kwargs["tools"] = tools
        try:
            raw = self._client_for(tier).chat.completions.create(**request_kwargs)
        except OpenAIError as exc:
            raise OpsAgentError(
                INF_8002_LLM_CALL_FAILED,
                f"LLM 调用失败: {exc}",
                {"tier": tier, "model": model_name},
            ) from exc

        if not raw.choices:
            raise OpsAgentError(
                INF_8002_LLM_CALL_FAILED,
                "LLM 返回空 choices",
                {"tier": tier, "model": model_name},
            )

        message = raw.choices[0].message
        tool_calls = [
            ToolCallOut(
                id=call.id,
                name=call.function.name,
                arguments_json=call.function.arguments,
            )
            for call in (message.tool_calls or [])
        ]
        usage = TokenUsage(
            prompt_tokens=raw.usage.prompt_tokens if raw.usage else 0,
            completion_tokens=raw.usage.completion_tokens if raw.usage else 0,
            total_tokens=raw.usage.total_tokens if raw.usage else 0,
        )
        _logger.info(
            "LLM 调用完成 model=%s tier=%s prompt_tokens=%s completion_tokens=%s",
            model_name,
            tier,
            usage.prompt_tokens,
            usage.completion_tokens,
        )
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=usage,
            model=model_name,
        )


class FakeLLMAdapter:
    """测试替身：按脚本顺序返回预设响应，记录全部调用入参供断言。"""

    def __init__(self, scripted: list[LLMResponse] | None = None) -> None:
        """初始化脚本队列。

        Args:
            scripted: 预设响应序列（可后续用 enqueue 追加）。
        """
        self._scripted: list[LLMResponse] = list(scripted or [])
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, response: LLMResponse) -> None:
        """追加一条预设响应。

        Args:
            response: 下一次 chat() 应返回的响应。
        """
        self._scripted.append(response)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: Tier = "deep",
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """按脚本顺序弹出响应并记录调用。

        Args:
            messages: 消息列表（原样记录）。
            tier: 模型档位（原样记录）。
            tools: 工具 schema（原样记录）。
            temperature: 采样温度（原样记录）。

        Returns:
            脚本中的下一条 LLMResponse。

        Raises:
            OpsAgentError: 脚本耗尽（错误码 INF-8002，测试断言用）。
        """
        self.calls.append(
            {"messages": messages, "tier": tier, "tools": tools, "temperature": temperature}
        )
        if not self._scripted:
            raise OpsAgentError(
                INF_8002_LLM_CALL_FAILED,
                "FakeLLM 脚本已耗尽：请为本次调用 enqueue 预设响应",
                {"calls_so_far": len(self.calls)},
            )
        return self._scripted.pop(0)


def create_llm_adapter(config: ModelConfig) -> LLMAdapter:
    """按 provider 构造 LLM 适配器（工厂入口，业务侧唯一获取途径）。

    Args:
        config: LLM 接入配置。

    Returns:
        provider=fake 时返回 FakeLLMAdapter，否则返回 OpenAICompatLLMAdapter。
    """
    if config.provider == "fake":
        return FakeLLMAdapter()
    return OpenAICompatLLMAdapter(config)
