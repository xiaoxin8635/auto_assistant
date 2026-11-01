"""infra/llm.py 单元测试：Fake 替身行为、key 缺失错误、工厂构造（全部无网络）。"""

from __future__ import annotations

import pytest

from opsagent.config import ModelConfig
from opsagent.errors import (
    INF_8002_LLM_CALL_FAILED,
    INF_8003_LLM_CONFIG_INVALID,
    OpsAgentError,
)
from opsagent.infra.llm import (
    FakeLLMAdapter,
    LLMResponse,
    OpenAICompatLLMAdapter,
    TokenUsage,
    create_llm_adapter,
)


def _scripted(content: str = "ok") -> LLMResponse:
    """构造测试用预设响应。

    Args:
        content: 文本回复内容。

    Returns:
        带 usage 的 LLMResponse。
    """
    return LLMResponse(
        content=content,
        usage=TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        model="deepseek-chat",
    )


def test_fake_returns_scripted_in_order() -> None:
    """按脚本顺序返回响应，并记录每次调用的 tier。"""
    fake = FakeLLMAdapter([_scripted("first"), _scripted("second")])
    assert fake.chat([{"role": "user", "content": "q"}]).content == "first"
    assert fake.chat([], tier="fast").content == "second"
    assert fake.calls[1]["tier"] == "fast"


def test_fake_records_call_context() -> None:
    """调用入参（tools/messages）原样记录供测试断言。"""
    fake = FakeLLMAdapter()
    fake.enqueue(_scripted())
    tools = [{"type": "function", "function": {"name": "query_metrics"}}]
    messages = [{"role": "user", "content": "查 CPU"}]
    fake.chat(messages, tools=tools)
    assert fake.calls[0]["messages"] == messages
    assert fake.calls[0]["tools"] == tools


def test_fake_exhausted_raises_inf_8002() -> None:
    """脚本耗尽时抛 OpsAgentError(INF-8002)，暴露测试脚本缺口的编码期错误。"""
    fake = FakeLLMAdapter([_scripted()])
    fake.chat([])
    with pytest.raises(OpsAgentError) as exc_info:
        fake.chat([])
    assert exc_info.value.code == INF_8002_LLM_CALL_FAILED


def test_missing_api_key_raises_inf_8003() -> None:
    """真实适配器在 key 未配置时构造客户端前即抛 INF-8003（不发网络请求）。"""
    adapter = OpenAICompatLLMAdapter(ModelConfig(api_key=""))
    with pytest.raises(OpsAgentError) as exc_info:
        adapter.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.code == INF_8003_LLM_CONFIG_INVALID


def test_factory_selects_by_provider() -> None:
    """工厂：fake -> FakeLLMAdapter；其余 provider -> OpenAICompatLLMAdapter。"""
    assert isinstance(
        create_llm_adapter(ModelConfig(provider="fake", api_key="x")), FakeLLMAdapter
    )
    assert isinstance(
        create_llm_adapter(ModelConfig(provider="deepseek", api_key="sk-test")),
        OpenAICompatLLMAdapter,
    )
