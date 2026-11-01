"""日志装配：统一格式、双输出（控制台 INFO / 文件 DEBUG 按天滚动保留 7 天）、敏感字段过滤。

规约要点（CODING_STANDARDS §6）：禁 print；格式含 task_id；password/key/token/secret 一律脱敏。
"""

from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 统一输出格式（task_id 经 extra 注入，无任务上下文时由过滤器补 "-"）
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | task=%(task_id)s | %(message)s"
LOG_FILE_NAME = "app.log"
LOG_ROTATE_WHEN = "midnight"
LOG_BACKUP_DAYS = 7

# 敏感字段模式：password/key/token/secret 后跟赋值符与取值的片段整体脱敏
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(password|passwd|api[_-]?key|secret|token)\s*[=:]\s*(\"|')?[^,;&\s\"']+"
)

# 命名空间根：只接管本项目的日志器，避免污染第三方库输出
ROOT_LOGGER_NAME = "opsagent"


class TaskIdDefaultFilter(logging.Filter):
    """为缺少 task_id 属性的日志记录补默认值，保证格式化永不抛 KeyError。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """补齐 task_id 属性。

        Args:
            record: 日志记录。

        Returns:
            恒为 True（本过滤器只补属性，不丢弃记录）。
        """
        if not hasattr(record, "task_id"):
            record.task_id = "-"
        return True


class SensitiveDataFilter(logging.Filter):
    """脱敏过滤器：将消息中 password/key/token/secret 赋值片段替换为 `***`。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """就地脱敏消息文本。

        Args:
            record: 日志记录。

        Returns:
            恒为 True（脱敏不丢弃记录）。
        """
        record.msg = _SENSITIVE_PATTERN.sub(r"\1=***", record.getMessage())
        record.args = ()
        return True


class TaskLogAdapter(logging.LoggerAdapter):
    """携带 task_id 上下文的日志适配器（工作流/工具层按任务维度埋点用）。"""

    def process(
        self, msg: object, kwargs: dict
    ) -> tuple[object, dict]:
        """合并适配器默认 extra 与调用点 extra（调用点可临时覆盖 task_id）。

        Args:
            msg: 日志消息。
            kwargs: 调用点关键字参数（可含 extra）。

        Returns:
            处理后的 (msg, kwargs) 二元组。
        """
        extra = dict(self.extra)
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str) -> logging.Logger:
    """获取 opsagent 命名空间子日志器（无任务上下文场景）。

    Args:
        name: 通常传 `__name__`。

    Returns:
        `opsagent.*` 命名空间下的 Logger。
    """
    if not name.startswith(ROOT_LOGGER_NAME):
        name = f"{ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def get_task_logger(name: str, task_id: str) -> TaskLogAdapter:
    """获取携带 task_id 的任务日志适配器。

    Args:
        name: 通常传 `__name__`。
        task_id: 当前工单任务 ID，贯穿日志与审计。

    Returns:
        TaskLogAdapter 实例。
    """
    return TaskLogAdapter(get_logger(name), {"task_id": task_id})


def setup_logging(level: str = "INFO", logs_dir: Path = Path("logs")) -> None:
    """装配项目日志（重复调用安全：先清空再挂载）。

    Args:
        level: 控制台日志级别（DEBUG/INFO/...）；文件日志固定 DEBUG。
        logs_dir: 日志目录（不存在则创建）。
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(LOG_FORMAT))

    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=logs_dir / LOG_FILE_NAME,
        when=LOG_ROTATE_WHEN,
        backupCount=LOG_BACKUP_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    for handler in (console, file_handler):
        handler.addFilter(TaskIdDefaultFilter())
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)
