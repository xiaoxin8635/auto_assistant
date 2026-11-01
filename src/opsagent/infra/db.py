"""SQLite 适配器：连接管理（WAL + 外键 + busy 超时）与通用读写。

只做连接与会话管理，不含任何业务表逻辑（表结构由 governance 审计与任务队列各自定义）。
引擎进程写、CLI 进程读，跨进程共享同一 SQLite 文件（WAL 模式支撑单写多读）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from opsagent.errors import (
    INF_8001_DB_OPERATION_FAILED,
    INF_8004_DB_SCHEMA_INIT_FAILED,
    OpsAgentError,
)

# 连接与会话参数（集中定义，避免魔法值散落）
_CONNECT_TIMEOUT_SECONDS = 5.0  # SQLite 文件锁等待上限
_BUSY_TIMEOUT_MS = 5000  # WAL 模式下写冲突 busy 等待上限


class Database:
    """SQLite 数据库访问对象（按需开短连接，事务自动提交/回滚）。

    Attributes:
        db_path: 数据库文件路径。
    """

    def __init__(self, db_path: Path) -> None:
        """初始化适配器（不立即建连，首次操作时打开）。

        Args:
            db_path: 数据库文件路径，父目录不存在时自动创建。
        """
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        """建立连接并设置会话级参数。

        Returns:
            配置好 Row 工厂 / WAL / 外键约束 / busy 超时的连接。

        Raises:
            OpsAgentError: 连接失败（错误码 INF-8001）。
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=_CONNECT_TIMEOUT_SECONDS)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            return conn
        except sqlite3.Error as exc:
            raise OpsAgentError(
                INF_8001_DB_OPERATION_FAILED,
                f"数据库连接失败: {exc}",
                {"db_path": str(self._db_path)},
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """连接上下文：正常退出自动提交，异常自动回滚，最终关闭连接。

        Yields:
            sqlite3 连接对象。

        Raises:
            OpsAgentError: 会话内任何 sqlite3.Error 统一包装为 INF-8001。
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise OpsAgentError(
                INF_8001_DB_OPERATION_FAILED,
                f"数据库操作失败: {exc}",
                {"db_path": str(self._db_path)},
            ) from exc
        finally:
            conn.close()

    def init_database(self, schema_sql: str) -> None:
        """批量执行建表脚本（幂等：脚本应使用 IF NOT EXISTS）。

        Args:
            schema_sql: 完整 DDL 脚本（可含多条语句，以分号分隔）。

        Raises:
            OpsAgentError: 脚本执行失败（错误码 INF-8004）。
        """
        try:
            conn = self._connect()
            try:
                conn.executescript(schema_sql)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise OpsAgentError(
                INF_8004_DB_SCHEMA_INIT_FAILED,
                f"数据库 schema 初始化失败: {exc}",
                {"db_path": str(self._db_path)},
            ) from exc

    def execute_write(self, sql: str, params: tuple | dict = ()) -> int:
        """执行单条写语句，返回自增主键。

        Args:
            sql: INSERT/UPDATE/DELETE 语句。
            params: 绑定参数（位置元组或命名字典）。

        Returns:
            最后插入行的 rowid（非 INSERT 语句返回 0）。

        Raises:
            OpsAgentError: 执行失败（错误码 INF-8001）。
        """
        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def execute_update(self, sql: str, params: tuple | dict = ()) -> int:
        """执行单条写语句，返回受影响行数（UPDATE/DELETE 计数用）。

        Args:
            sql: UPDATE/DELETE 语句。
            params: 绑定参数。

        Returns:
            受影响行数。

        Raises:
            OpsAgentError: 执行失败（错误码 INF-8001）。
        """
        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    def query_all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        """执行只读查询，返回全部行。

        Args:
            sql: SELECT 语句。
            params: 绑定参数。

        Returns:
            行字典列表（按查询顺序）。

        Raises:
            OpsAgentError: 执行失败（错误码 INF-8001）。
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        """执行只读查询，返回首行。

        Args:
            sql: SELECT 语句。
            params: 绑定参数。

        Returns:
            首行字典；无结果时返回 None。

        Raises:
            OpsAgentError: 执行失败（错误码 INF-8001）。
        """
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
