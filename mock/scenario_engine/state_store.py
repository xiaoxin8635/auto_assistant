"""mock 共享状态库（ADR-0006）：SQLite WAL，四个服务进程共享同一份模拟状态。

四张表：mock_host_epochs（区间时代线，追加写）、mock_host_meta（活动场景与恢复锚点）、
mock_logs（模拟日志）、mock_tickets（模拟工单）。短连接 + busy_timeout，
写冲突概率低（P1 规模），进程间通过文件锁由 SQLite 保证一致性。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mock.scenario_engine.errors import MOCK_4007_STATE_STORE_ERROR, ScenarioError
from mock.scenario_engine.metrics import RangeEpoch

DEFAULT_DB_PATH = Path("data/mock_state.sqlite3")
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mock_host_epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL,
    since REAL NOT NULL,
    state TEXT NOT NULL,
    ranges_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_epochs_host ON mock_host_epochs(host_id, since);
CREATE TABLE IF NOT EXISTS mock_host_meta (
    host_id TEXT PRIMARY KEY,
    active_scenario TEXT,
    fix_at REAL
);
CREATE TABLE IF NOT EXISTS mock_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    host_id TEXT NOT NULL,
    service TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_host ON mock_logs(host_id, timestamp);
CREATE TABLE IF NOT EXISTS mock_tickets (
    ticket_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    closed_at REAL
);
"""


class MockStateStore:
    """SQLite 共享状态库连接封装（短连接，异常统一映射 MOCK-4007）。"""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        """绑定数据库文件（不立即建连）。

        Args:
            db_path: SQLite 文件路径（父目录不存在时 init_schema 自动创建）。
        """
        self._db_path = db_path

    def init_schema(self) -> None:
        """建库建表（幂等；父目录不存在则创建）。

        Raises:
            ScenarioError: 建库失败（MOCK-4007）。
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as conn:
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise ScenarioError(
                MOCK_4007_STATE_STORE_ERROR, f"mock 状态库初始化失败: {exc}"
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """短连接上下文：WAL + busy_timeout，正常提交、异常回滚并关闭。

        Yields:
            sqlite3 连接。

        Raises:
            ScenarioError: 底层 sqlite3.Error 统一映射为 MOCK-4007。
        """
        try:
            conn = sqlite3.connect(self._db_path, timeout=BUSY_TIMEOUT_MS / 1000)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
            try:
                yield conn
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise ScenarioError(
                MOCK_4007_STATE_STORE_ERROR, f"mock 状态库操作失败: {exc}"
            ) from exc

    # ---------- 时代线 ----------

    def append_epoch(self, host_id: str, since: float, state: str, ranges: dict[str, str]) -> None:
        """追加区间时代片段（自 since 起该状态生效）。

        Args:
            host_id: 主机 ID。
            since: 生效起始时刻。
            state: 状态标识。
            ranges: 指标区间字典。
        """
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO mock_host_epochs(host_id, since, state, ranges_json) VALUES(?, ?, ?, ?)",
                (host_id, since, state, json.dumps(ranges, ensure_ascii=False)),
            )

    def get_epochs(self, host_id: str) -> list[RangeEpoch]:
        """读取主机时代线（since 升序）。

        Args:
            host_id: 主机 ID。

        Returns:
            RangeEpoch 列表；主机从未登记时为空列表。
        """
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT since, state, ranges_json FROM mock_host_epochs WHERE host_id=? ORDER BY since",
                (host_id,),
            ).fetchall()
        return [
            RangeEpoch(since=row[0], state=row[1], ranges=json.loads(row[2]))
            for row in rows
        ]

    def count_epochs(self, host_id: str) -> int:
        """统计主机时代片段数（基线惰性登记的幂等判据）。

        Args:
            host_id: 主机 ID。

        Returns:
            片段数量。
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM mock_host_epochs WHERE host_id=?", (host_id,)
            ).fetchone()
        return int(row[0])

    # ---------- 主机元数据 ----------

    def set_meta(self, host_id: str, active_scenario: str | None, fix_at: float | None) -> None:
        """写入主机元数据（UPSERT）。

        Args:
            host_id: 主机 ID。
            active_scenario: 活动场景 ID。
            fix_at: 最近一次修复生效时刻（恢复锚点）。
        """
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO mock_host_meta(host_id, active_scenario, fix_at)
                VALUES(?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    active_scenario=excluded.active_scenario,
                    fix_at=excluded.fix_at
                """,
                (host_id, active_scenario, fix_at),
            )

    def get_meta(self, host_id: str) -> dict[str, Any] | None:
        """读取主机元数据。

        Args:
            host_id: 主机 ID。

        Returns:
            {active_scenario, fix_at}；从未写入过时 None。
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT active_scenario, fix_at FROM mock_host_meta WHERE host_id=?",
                (host_id,),
            ).fetchone()
        if row is None:
            return None
        return {"active_scenario": row[0], "fix_at": row[1]}

    # ---------- 日志 ----------

    def add_log(
        self, host_id: str, service: str, level: str, message: str, timestamp: float
    ) -> None:
        """写入一条模拟日志。

        Args:
            host_id: 主机 ID。
            service: 服务名。
            level: 日志级别（INFO/ERROR）。
            message: 正文。
            timestamp: 时刻。
        """
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO mock_logs(timestamp, host_id, service, level, message) VALUES(?, ?, ?, ?, ?)",
                (timestamp, host_id, service, level, message),
            )

    def search_logs(
        self,
        host_id: str,
        service: str | None = None,
        keyword: str = "",
        lines: int = 20,
    ) -> list[dict[str, Any]]:
        """按主机/服务/关键字检索日志（关键字大小写不敏感，时间倒序）。

        Args:
            host_id: 主机 ID。
            service: 服务过滤；None 不过滤。
            keyword: 正文包含关键字；空串匹配全部。
            lines: 最大返回条数。

        Returns:
            日志字典列表（时间倒序）。
        """
        sql = (
            "SELECT timestamp, host_id, service, level, message FROM mock_logs "
            "WHERE host_id=? AND lower(message) LIKE ?"
        )
        params: list[Any] = [host_id, f"%{keyword.lower()}%"]
        if service is not None:
            sql += " AND service=?"
            params.append(service)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(lines)
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "timestamp": row[0],
                "host_id": row[1],
                "service": row[2],
                "level": row[3],
                "message": row[4],
            }
            for row in rows
        ]

    # ---------- 工单 ----------

    def create_ticket(self, title: str, content: str = "") -> dict[str, Any]:
        """创建模拟工单（TK-6 位自增编号）。

        Args:
            title: 工单标题。
            content: 工单描述。

        Returns:
            工单字典。
        """
        now = time.time()
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM mock_tickets").fetchone()
            ticket_id = f"TK-{int(row[0]) + 1:06d}"
            conn.execute(
                "INSERT INTO mock_tickets(ticket_id, title, content, status, created_at) VALUES(?, ?, ?, 'open', ?)",
                (ticket_id, title, content, now),
            )
        return {
            "ticket_id": ticket_id,
            "title": title,
            "content": content,
            "status": "open",
            "created_at": now,
        }

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        """查询工单；不存在返回 None。

        Args:
            ticket_id: 工单 ID。

        Returns:
            工单字典或 None。
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT ticket_id, title, content, status, created_at, closed_at FROM mock_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "ticket_id": row[0],
            "title": row[1],
            "content": row[2],
            "status": row[3],
            "created_at": row[4],
            "closed_at": row[5],
        }

    def close_ticket(self, ticket_id: str) -> bool:
        """闭环工单；工单不存在返回 False。

        Args:
            ticket_id: 工单 ID。

        Returns:
            是否闭环成功。
        """
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE mock_tickets SET status='closed', closed_at=? WHERE ticket_id=?",
                (time.time(), ticket_id),
            )
            return cursor.rowcount > 0

    # ---------- 复位 ----------

    def reset_all(self) -> None:
        """清空全部模拟状态（时代线/元数据/日志/工单）。"""
        with self.connection() as conn:
            conn.execute("DELETE FROM mock_host_epochs")
            conn.execute("DELETE FROM mock_host_meta")
            conn.execute("DELETE FROM mock_logs")
            conn.execute("DELETE FROM mock_tickets")


__all__ = ["DEFAULT_DB_PATH", "MockStateStore"]
