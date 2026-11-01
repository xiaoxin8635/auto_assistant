"""infra/db.py 单元测试：连接参数、schema 初始化、CRUD、错误包装、事务回滚。"""

from __future__ import annotations

import pytest

from opsagent.errors import (
    INF_8001_DB_OPERATION_FAILED,
    INF_8004_DB_SCHEMA_INIT_FAILED,
    OpsAgentError,
)
from opsagent.infra.db import Database

_TEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued'
);
"""


def test_connection_pragmas(tmp_db: Database) -> None:
    """会话级参数：WAL 日志模式 + 外键约束开启（跨进程共享的语义基础）。"""
    with tmp_db.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_schema_and_crud_roundtrip(tmp_db: Database) -> None:
    """建库、插入（返回自增主键）、单查、全查、更新（返回行数）。"""
    tmp_db.init_database(_TEST_SCHEMA)
    row_id = tmp_db.execute_write(
        "INSERT INTO tasks (task_id) VALUES (?)", ("TASK-0001",)
    )
    assert row_id == 1

    row = tmp_db.query_one("SELECT * FROM tasks WHERE task_id = ?", ("TASK-0001",))
    assert row is not None
    assert row["status"] == "queued"

    assert len(tmp_db.query_all("SELECT * FROM tasks")) == 1
    assert tmp_db.query_one("SELECT * FROM tasks WHERE task_id = ?", ("NOPE",)) is None

    changed = tmp_db.execute_update(
        "UPDATE tasks SET status = ? WHERE task_id = ?", ("running", "TASK-0001")
    )
    assert changed == 1
    assert tmp_db.query_one("SELECT * FROM tasks WHERE task_id = ?", ("TASK-0001",))[
        "status"
    ] == "running"


def test_write_error_wrapped_as_inf_8001(tmp_db: Database) -> None:
    """SQL 执行失败统一包装为 OpsAgentError(INF-8001)，不向调用方泄漏裸 sqlite3 异常。"""
    with pytest.raises(OpsAgentError) as exc_info:
        tmp_db.execute_write("INSERT INTO no_such_table (a) VALUES (1)")
    assert exc_info.value.code == INF_8001_DB_OPERATION_FAILED


def test_schema_init_error_wrapped_as_inf_8004(tmp_db: Database) -> None:
    """DDL 脚本失败包装为 OpsAgentError(INF-8004)。"""
    with pytest.raises(OpsAgentError) as exc_info:
        tmp_db.init_database("CREATE TABLE broken (")  # 语法错误
    assert exc_info.value.code == INF_8004_DB_SCHEMA_INIT_FAILED


def test_rollback_on_session_error(tmp_db: Database) -> None:
    """会话内非 SQL 异常退出时自动回滚，不残留半截写入。"""
    tmp_db.init_database(_TEST_SCHEMA)
    with pytest.raises(RuntimeError):
        with tmp_db.connection() as conn:
            conn.execute("INSERT INTO tasks (task_id) VALUES (?)", ("TASK-X",))
            raise RuntimeError("boom")
    assert tmp_db.query_one("SELECT * FROM tasks WHERE task_id = ?", ("TASK-X",)) is None
