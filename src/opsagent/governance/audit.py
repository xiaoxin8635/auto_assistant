"""治理审计：SQLite 四表（tasks / node_runs / llm_calls / tool_calls）写入与任务时间线导出。

规约（DESIGN §5.6）：
- 全部明细表带 task_id 外键引用任务主档——主档必须先建，外键约束保证审计不出现孤儿记录
- `export_timeline` 输出完整可读时间线，供 `opsagent audit <task_id>` 与评测判定消费
- 写失败经 infra/db 统一映射 INF-8001；任务不存在统一 GOV-5002
"""

from __future__ import annotations

import enum
import json
import time
from typing import Any, Callable

from opsagent.errors import GOV_5002_TASK_NOT_FOUND, OpsAgentError
from opsagent.infra.db import Database
from opsagent.tools.base import ToolResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    alert_id TEXT,
    alert_raw TEXT NOT NULL,
    status TEXT NOT NULL,
    scenario_id TEXT,
    final_report TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS node_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_name TEXT NOT NULL,
    round_no INTEGER NOT NULL DEFAULT 0,
    input_json TEXT,
    output_json TEXT,
    elapsed_ms REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_runs_task ON node_runs(task_id, created_at);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_name TEXT NOT NULL,
    tier TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_json TEXT NOT NULL,
    response_text TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    elapsed_ms REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task_id, created_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    params_json TEXT NOT NULL,
    success INTEGER NOT NULL,
    error_code TEXT,
    elapsed_ms REAL NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id, created_at);
"""


class TaskStatus(str, enum.Enum):
    """任务生命周期状态（DESIGN §5.5.2：终态 closed/escalated/failed，waiting_human 超时后转 escalate）。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    CLOSED = "closed"
    ESCALATED = "escalated"
    FAILED = "failed"


# 触发 closed_at 落库的终态集合（closed_at 只记首次到达，不因后续更新回退）
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.CLOSED, TaskStatus.ESCALATED, TaskStatus.FAILED}
)


def _dump(value: Any) -> str:
    """序列化为 JSON 文本（ensure_ascii=False 保持中文可读；不可序列化对象降级 str）。

    Args:
        value: 任意 JSON 兼容对象。

    Returns:
        JSON 字符串。
    """
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(text: str | None) -> Any:
    """反序列化 JSON 文本（空值安全）。

    Args:
        text: JSON 字符串或 None。

    Returns:
        解析后的对象；输入为空时 None。
    """
    return json.loads(text) if text else None


class AuditLogger:
    """审计日志读写器：任务主档 + 三张明细表 + 时间线导出。

    Attributes:
        _db: 底层 SQLite 适配器（复用其连接管理与错误映射）。
        _clock: 时间源（测试注入假时钟）。
    """

    def __init__(self, db: Database, *, clock: Callable[[], float] | None = None) -> None:
        """绑定数据库并确保四表存在（幂等建表）。

        Args:
            db: SQLite 适配器。
            clock: 时间源；缺省真实时间。
        """
        self._db = db
        self._clock = clock or time.time
        self._db.init_database(_SCHEMA)

    # ---------- 任务主档 ----------

    def create_task(
        self,
        task_id: str,
        alert_raw: dict[str, Any],
        *,
        alert_id: str | None = None,
        scenario_id: str | None = None,
        status: TaskStatus = TaskStatus.RUNNING,
    ) -> None:
        """创建任务主档（审计明细的前置依赖）。

        Args:
            task_id: 全局唯一任务 ID。
            alert_raw: 原始告警 JSON。
            alert_id: 告警源 ID（可选）。
            scenario_id: 关联场景 ID（评测回溯用，可选）。
            status: 初始状态（FIFO 排队场景传 QUEUED）。

        Raises:
            OpsAgentError: 主档 ID 重复或写库失败（INF-8001）。
        """
        now = self._clock()
        self._db.execute_write(
            "INSERT INTO tasks(task_id, alert_id, alert_raw, status, scenario_id, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (task_id, alert_id, _dump(alert_raw), status.value, scenario_id, now, now),
        )

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        final_report: dict[str, Any] | None = None,
    ) -> None:
        """更新任务状态（终态落 closed_at；final_report 只在提供时覆盖）。

        Args:
            task_id: 任务 ID。
            status: 新状态。
            final_report: 处理报告（闭环/转人工时附上）。

        Raises:
            OpsAgentError: 任务不存在（GOV-5002）。
        """
        now = self._clock()
        report_json = _dump(final_report) if final_report is not None else None
        closed_at = now if status in _TERMINAL_STATUSES else None
        affected = self._db.execute_update(
            "UPDATE tasks SET status = ?, updated_at = ?,"
            " final_report = COALESCE(?, final_report),"
            " closed_at = COALESCE(closed_at, ?)"
            " WHERE task_id = ?",
            (status.value, now, report_json, closed_at, task_id),
        )
        if affected == 0:
            raise OpsAgentError(
                GOV_5002_TASK_NOT_FOUND, f"任务不存在: {task_id}", {"task_id": task_id}
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """查询任务主档（JSON 列已解析）。

        Args:
            task_id: 任务 ID。

        Returns:
            主档字典；不存在时 None。
        """
        row = self._db.query_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            return None
        row["alert_raw"] = _load(row["alert_raw"])
        row["final_report"] = _load(row["final_report"])
        return row

    def list_tasks(self, *, status: TaskStatus | None = None) -> list[dict[str, Any]]:
        """列出任务（轻量列，不含 alert_raw / final_report；时间倒序）。

        Args:
            status: 状态过滤；None 列全部。

        Returns:
            任务摘要列表。
        """
        sql = "SELECT task_id, alert_id, status, scenario_id, created_at, updated_at, closed_at FROM tasks"
        params: tuple = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status.value,)
        sql += " ORDER BY created_at DESC, task_id DESC"
        return self._db.query_all(sql, params)

    # ---------- 节点执行 ----------

    def record_node(
        self,
        task_id: str,
        node_name: str,
        *,
        round_no: int = 0,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        elapsed_ms: float = 0.0,
    ) -> None:
        """记录一次节点执行（出入参摘要与耗时）。

        Args:
            task_id: 任务 ID。
            node_name: 节点名（triage/collect/analyze/execute/validate）。
            round_no: 工作流轮次。
            input: 入参摘要。
            output: 出参摘要。
            elapsed_ms: 耗时。

        Raises:
            OpsAgentError: 任务主档不存在（外键约束，INF-8001）或写库失败。
        """
        self._db.execute_write(
            "INSERT INTO node_runs(task_id, node_name, round_no, input_json, output_json, elapsed_ms, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (task_id, node_name, round_no, _dump(input), _dump(output), elapsed_ms, self._clock()),
        )

    # ---------- LLM 调用 ----------

    def record_llm(
        self,
        task_id: str,
        node_name: str,
        *,
        tier: str,
        model: str,
        messages: list[dict[str, Any]],
        response_text: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        elapsed_ms: float = 0.0,
        error_code: str | None = None,
    ) -> None:
        """记录一次 LLM 调用（prompt/响应/token/模型版本，成本与评测回溯依据）。

        Args:
            task_id: 任务 ID。
            node_name: 发起调用的节点。
            tier: 模型档位（fast/deep）。
            model: 模型名（版本快照）。
            messages: 完整消息序列。
            response_text: 响应文本（失败时为空）。
            prompt_tokens: 输入 token 数。
            completion_tokens: 输出 token 数。
            elapsed_ms: 耗时。
            error_code: 失败错误码（成功为 None）。

        Raises:
            OpsAgentError: 任务主档不存在或写库失败。
        """
        self._db.execute_write(
            "INSERT INTO llm_calls(task_id, node_name, tier, model, prompt_json, response_text,"
            " prompt_tokens, completion_tokens, elapsed_ms, error_code, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                node_name,
                tier,
                model,
                _dump(messages),
                response_text,
                prompt_tokens,
                completion_tokens,
                elapsed_ms,
                error_code,
                self._clock(),
            ),
        )

    # ---------- 工具调用 ----------

    def record_tool(
        self,
        task_id: str,
        node_name: str,
        tool_name: str,
        *,
        params: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """记录一次工具调用（直接消费 ToolResult，错误码/耗时/结果统一落库）。

        Args:
            task_id: 任务 ID。
            node_name: 发起调用的节点。
            tool_name: 工具名。
            params: 调用参数。
            result: 工具返回结构。

        Raises:
            OpsAgentError: 任务主档不存在或写库失败。
        """
        self._db.execute_write(
            "INSERT INTO tool_calls(task_id, node_name, tool_name, params_json, success,"
            " error_code, elapsed_ms, result_json, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                node_name,
                tool_name,
                _dump(params),
                1 if result.success else 0,
                result.error_code,
                result.elapsed_ms,
                _dump(result.data),
                self._clock(),
            ),
        )

    # ---------- 时间线导出 ----------

    def export_timeline(self, task_id: str) -> dict[str, Any]:
        """导出任务完整审计时间线（主档 + 三张明细，时间升序，JSON 全部还原为对象）。

        Args:
            task_id: 任务 ID。

        Returns:
            {task, node_runs, llm_calls, tool_calls}。

        Raises:
            OpsAgentError: 任务不存在（GOV-5002）。
        """
        task = self.get_task(task_id)
        if task is None:
            raise OpsAgentError(
                GOV_5002_TASK_NOT_FOUND, f"任务不存在: {task_id}", {"task_id": task_id}
            )
        node_runs = self._query_detail(
            task_id, "node_runs", {"input": "input_json", "output": "output_json"}
        )
        llm_calls = self._query_detail(task_id, "llm_calls", {"messages": "prompt_json"})
        tool_calls = self._query_detail(
            task_id, "tool_calls", {"params": "params_json", "result": "result_json"}
        )
        return {
            "task": task,
            "node_runs": node_runs,
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
        }

    def _query_detail(
        self, task_id: str, table: str, parse_fields: dict[str, str]
    ) -> list[dict[str, Any]]:
        """查明细表并把 JSON 列还原为对象（时间升序；原始 *_json 列不导出）。

        Args:
            task_id: 任务 ID。
            table: 明细表名（内部常量，非用户输入）。
            parse_fields: 输出字段名 -> JSON 列名。

        Returns:
            行列表（时间升序）。
        """
        rows = self._db.query_all(
            f"SELECT * FROM {table} WHERE task_id = ? ORDER BY created_at, id", (task_id,)
        )
        for row in rows:
            for field, column in parse_fields.items():
                row[field] = _load(row.pop(column))
        return rows


__all__ = ["AuditLogger", "TaskStatus"]
