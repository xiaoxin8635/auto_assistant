"""场景引擎内部异常：镜像 src 错误码格式（MOCK 4xxx 段），但不依赖 src/opsagent。"""

from __future__ import annotations

from typing import Any

# 与 src/opsagent/errors.py 的 MOCK 段保持同码（规约：mock 域不得反向 import 业务代码，故本地镜像常量）
MOCK_4001_SCENARIO_NOT_FOUND = "MOCK-4001"
MOCK_4004_STATE_INVALID = "MOCK-4004"
MOCK_4005_DATA_INVALID = "MOCK-4005"
MOCK_4006_HOST_NOT_FOUND = "MOCK-4006"
MOCK_4007_STATE_STORE_ERROR = "MOCK-4007"


class ScenarioError(Exception):
    """场景引擎域内异常。

    Attributes:
        code: 错误码（MOCK-4xxx）。
        message: 人类可读描述（中文）。
        detail: 结构化附加信息。
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
