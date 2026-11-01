"""基础设施适配层（ADR-0005）：db / vector / llm / embedding 四类外部依赖的统一接入点。

上层（workflow / tools / rag）只依赖本包接口，禁止直接 import openai / chromadb / sqlite3 等外部 SDK。
"""

from opsagent.infra.db import Database
from opsagent.infra.llm import (
    FakeLLMAdapter,
    LLMAdapter,
    LLMResponse,
    OpenAICompatLLMAdapter,
    Tier,
    TokenUsage,
    ToolCallOut,
    create_llm_adapter,
)

__all__ = [
    "Database",
    "FakeLLMAdapter",
    "LLMAdapter",
    "LLMResponse",
    "OpenAICompatLLMAdapter",
    "Tier",
    "TokenUsage",
    "ToolCallOut",
    "create_llm_adapter",
]
