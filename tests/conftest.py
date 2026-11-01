"""pytest 公共夹具：隔离的临时数据库等。"""

from __future__ import annotations

from pathlib import Path

import pytest

from opsagent.infra.db import Database


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    """创建随用例销毁的临时 SQLite 数据库。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        指向临时文件的 Database 实例。
    """
    return Database(tmp_path / "opsagent.sqlite3")
