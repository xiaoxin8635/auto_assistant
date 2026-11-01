"""统一异常体系与错误码常量。

错误码格式：`模块段-4位数字`（如 INF-8001），模块段与 CODING_STANDARDS §5.1 对齐；
所有业务异常必须继承 OpsAgentError，禁止在业务层抛裸 Exception。
"""

from __future__ import annotations

import re
from typing import Any

# 错误码模块段（与 CODING_STANDARDS §5.1 错误码表一一对应）
SECTION_TOOL = "TOOL"  # 工具层 1xxx
SECTION_WF = "WF"  # 工作流 3xxx
SECTION_MOCK = "MOCK"  # 模拟服务 4xxx
SECTION_GOV = "GOV"  # 治理 5xxx
SECTION_RAG = "RAG"  # 知识检索 6xxx
SECTION_ACC = "ACC"  # 接入层 7xxx
SECTION_INF = "INF"  # 基础设施适配层 8xxx

# 工具层段（TOOL 1xxx）
TOOL_1001_TOOL_CALL_FAILED = "TOOL-1001"  # 工具调用模拟服务失败（超时/网络/上游错误）
TOOL_1002_TOOL_PARAM_INVALID = "TOOL-1002"  # 工具参数校验失败
TOOL_1003_TOOL_NOT_FOUND = "TOOL-1003"  # 注册表中不存在该工具

# 治理段（GOV 5xxx）
GOV_5002_TASK_NOT_FOUND = "GOV-5002"  # 任务主档不存在（更新/导出时）

# 工作流段（WF 3xxx）
WF_3001_ANALYSIS_OUTPUT_INVALID = "WF-3001"  # 结构化解析失败（告警受理/根因分析输出非 JSON 或缺关键字段）
WF_3002_MAX_ROUNDS_EXCEEDED = "WF-3002"  # 全局轮次超限，强制终止
WF_3003_TOKEN_BUDGET_EXCEEDED = "WF-3003"  # token 预算熔断，转人工（保留证据价值）

# 基础设施段（INF 8xxx）——infra/ 适配器专用
INF_8001_DB_OPERATION_FAILED = "INF-8001"  # 数据库读写失败
INF_8002_LLM_CALL_FAILED = "INF-8002"  # LLM 调用失败（超时/网络/上游错误）
INF_8003_LLM_CONFIG_INVALID = "INF-8003"  # LLM 配置缺失（如 API key 未填）
INF_8004_DB_SCHEMA_INIT_FAILED = "INF-8004"  # 数据库 schema 初始化失败

# 接入层段（ACC 7xxx）
ACC_7001_ALERT_FORMAT_INVALID = "ACC-7001"  # 告警 JSON 格式非法

# 模拟服务段（MOCK 4xxx）
MOCK_4001_SCENARIO_NOT_FOUND = "MOCK-4001"  # 场景 ID 不存在
MOCK_4002_INJECT_DELAY = "MOCK-4002"  # 注入延迟生效
MOCK_4003_INJECT_FAILURE = "MOCK-4003"  # 注入失败生效（模拟 5xx）

_ERROR_CODE_PATTERN = re.compile(r"^[A-Z]{2,4}-\d{4}$")


class OpsAgentError(Exception):
    """项目统一业务异常基类。

    Attributes:
        code: 错误码，格式 `XXX-1234`（模块段大写 2~4 字母 + 4 位数字；段宽见 §5.1 错误码表，如 WF/TOOL）。
        message: 人类可读的错误描述（中文）。
        detail: 结构化附加信息（失败参数、上游响应摘要等），可为空。

    Raises:
        ValueError: 错误码格式不合法（编码期缺陷，直接暴露不吞掉）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if not _ERROR_CODE_PATTERN.match(code):
            raise ValueError(f"错误码格式非法: {code!r}，应为 XXX-1234（模块段 2~4 位大写字母 + 4 位数字）")
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    def to_dict(self) -> dict[str, Any]:
        """序列化为结构化错误体（供 ToolResult / HTTP 响应复用）。

        Returns:
            包含 code / message / detail 三键的字典。
        """
        return {"code": self.code, "message": self.message, "detail": self.detail}
