"""配置加载：环境变量 > config/model.yaml > 代码默认值。

所有配置经 pydantic 模型校验后使用；业务代码禁止散落魔法值（超时/预算/端口/路径一律进本模块）。
密钥仅存于 config/model.yaml（gitignored）或环境变量，严禁入库、入日志。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# 默认配置文件相对项目根（运行工作目录）路径；不存在时全部走默认值
DEFAULT_YAML_FILE = "config/model.yaml"


class ModelConfig(BaseModel):
    """LLM 接入配置（OpenAI 兼容协议，DeepSeek / 通义千问 / fake 均可）。

    Attributes:
        provider: 厂商标识。`fake` 表示本地脚本化响应适配器（联调免 key）。
        base_url: OpenAI 兼容 API 地址。
        api_key: API 密钥（SecretStr，防止日志/_repr 泄漏）。
        model_fast: fast tier 模型名（提取/摘要类轻量调用）。
        model_deep: deep tier 模型名（诊断推理类调用；需支持 function calling）。
        timeout_seconds: 单次 HTTP 调用超时（秒）。
        max_retries: 传输层重试次数（openai SDK 内建指数退避）。
        temperature: 采样温度。
    """

    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key: SecretStr = SecretStr("")
    model_fast: str = "deepseek-chat"
    model_deep: str = "deepseek-chat"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    temperature: float = 0.2


class ToolConfig(BaseModel):
    """工具层 HTTP 调用配置。

    Attributes:
        timeout_seconds: 工具调用模拟服务的 HTTP 超时（秒）。
        max_retries: 传输层（超时/5xx/429）指数退避重试上限。
    """

    timeout_seconds: float = 5.0
    max_retries: int = 2


class WorkflowConfig(BaseModel):
    """工作流循环与预算控制参数。

    Attributes:
        max_rounds: 全局轮次上限（三重终止之一，超限强制 failed）。
        no_progress_limit: 连续"无进展"判定阈值（相同工具+相同参数+无新证据）。
        token_budget_default: 默认单任务 token 预算（场景 YAML 可覆盖）。
    """

    max_rounds: int = 15
    no_progress_limit: int = 2
    token_budget_default: int = 8000


class MockConfig(BaseModel):
    """模拟业务系统四服务的地址参数（端口约定见 DESIGN.md §5.2）。

    Attributes:
        host: 模拟服务监听地址。
        monitor_port: mock-monitor（指标查询）端口。
        logs_port: mock-logs（日志检索）端口。
        cmdb_port: mock-cmdb（资产信息）端口。
        ticket_port: mock-ticket（工单操作）端口。
    """

    host: str = "127.0.0.1"
    monitor_port: int = 8101
    logs_port: int = 8102
    cmdb_port: int = 8103
    ticket_port: int = 8104


class Settings(BaseSettings):
    """全局配置聚合根。

    加载优先级：init kwargs > 环境变量（前缀 `OPSAGENT_`，嵌套用 `__`）> yaml > 默认值。

    Attributes:
        model: LLM 接入配置。
        tools: 工具层调用配置。
        workflow: 工作流循环与预算参数。
        mock: 模拟服务地址参数。
        data_dir: 运行时数据根目录（SQLite 数据库等）。
        logs_dir: 日志根目录。
        log_level: 控制台日志级别（文件日志固定 DEBUG）。
    """

    model: ModelConfig = Field(default_factory=ModelConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    mock: MockConfig = Field(default_factory=MockConfig)
    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="OPSAGENT_",
        env_nested_delimiter="__",
        yaml_file=DEFAULT_YAML_FILE,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """自定义配置源优先级：init > 环境变量 > yaml > 默认值（规约 §7）。

        Args:
            settings_cls: Settings 类本身。
            init_settings: 初始化参数源（最高优先级）。
            env_settings: 环境变量源。
            dotenv_settings: .env 文件源（本项目未使用，保持缺省位次）。
            file_secret_settings: 密钥文件源（本项目未使用）。

        Returns:
            按优先级从高到低排列的配置源元组。
        """
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级配置单例（应用入口使用；测试请直接构造 Settings）。

    Returns:
        全局 Settings 实例（按 环境变量 > yaml > 默认值 装配）。
    """
    return Settings()


def get_db_path(settings: Settings) -> Path:
    """解析 SQLite 数据库文件路径。

    Args:
        settings: 全局配置。

    Returns:
        数据库文件路径（`<data_dir>/opsagent.sqlite3`）。
    """
    return settings.data_dir / "opsagent.sqlite3"
