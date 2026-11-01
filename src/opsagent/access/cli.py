"""CLI 接入层：opsagent submit / status / audit 三命令（DESIGN §5.1 P1 形态）。

表现层零业务逻辑：本模块只做参数解析、组件装配与结果渲染；
诊断编排在 workflow/，审计读写在 governance/，装配复用 config/tools/infra 工厂。
review（人工审核）命令与 FIFO 队列消费者随 P2 human_review 节点引入。

用法：
    opsagent submit alerts/alert.json   # 提交告警，前台同步执行一次诊断
    opsagent submit --inline '<json>'   # 内联 JSON 提交（脚本/联调用）
    opsagent status [task_id]           # 无参列出最近任务；有参看单任务详情
    opsagent audit <task_id> [--json]   # 审计时间线渲染；--json 输出原始数据（评测消费）

退出码约定：closed=0；escalated/failed=1（非闭环，CI 可感知）；参数/输入/查询错误=2。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from opsagent.config import Settings, WorkflowConfig, get_db_path, get_settings
from opsagent.errors import ACC_7001_ALERT_FORMAT_INVALID, GOV_5002_TASK_NOT_FOUND, OpsAgentError
from opsagent.governance.audit import AuditLogger
from opsagent.infra.db import Database
from opsagent.infra.llm import LLMAdapter, create_llm_adapter
from opsagent.tools import build_default_registry
from opsagent.tools.base import ToolRegistry
from opsagent.tools.recovery import RecoveryCheckTool, build_recovery_tool
from opsagent.workflow.graph import build_diagnosis_graph, run_task


@dataclass
class CliComponents:
    """CLI 全量依赖组件（测试注入替身的统一入口）。

    Attributes:
        settings: 全局配置。
        llm: LLM 适配器。
        registry: LLM 可见工具注册表（含高危修复工具）。
        recovery: 恢复判定工具（工作流内部持有）。
        audit: 审计器。
        cfg: 工作流配置。
    """

    settings: Settings
    llm: LLMAdapter
    registry: ToolRegistry
    recovery: RecoveryCheckTool
    audit: AuditLogger
    cfg: WorkflowConfig


def build_components(settings: Settings | None = None) -> CliComponents:
    """按配置装配 CLI 全量组件（生产路径；测试用 CliComponents 直接构造替身）。

    Args:
        settings: 全局配置；None 时取进程单例。

    Returns:
        CliComponents。
    """
    settings = settings or get_settings()
    audit = AuditLogger(Database(get_db_path(settings)))
    return CliComponents(
        settings=settings,
        llm=create_llm_adapter(settings.model),
        registry=build_default_registry(settings),
        recovery=build_recovery_tool(settings),
        audit=audit,
        cfg=settings.workflow,
    )


def _load_alert(args: argparse.Namespace) -> dict[str, Any]:
    """从文件或 --inline 读取告警 JSON（接入层输入边界，非法即 ACC-7001）。

    Args:
        args: 解析后的命令行参数。

    Returns:
        告警 dict。

    Raises:
        OpsAgentError: 缺少输入、文件读取失败、JSON 非法或非对象（ACC-7001）。
    """
    inline = getattr(args, "inline", None)
    alert_file = getattr(args, "alert_file", None)
    if not inline and not alert_file:
        raise OpsAgentError(
            ACC_7001_ALERT_FORMAT_INVALID, "必须提供告警文件路径或 --inline JSON"
        )
    if inline:
        text = inline
    else:
        try:
            text = Path(str(alert_file)).read_text(encoding="utf-8")
        except OSError as exc:
            raise OpsAgentError(
                ACC_7001_ALERT_FORMAT_INVALID, f"告警文件读取失败: {exc}"
            ) from exc
    try:
        alert = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpsAgentError(
            ACC_7001_ALERT_FORMAT_INVALID, f"告警 JSON 解析失败: {exc}"
        ) from exc
    if not isinstance(alert, dict):
        raise OpsAgentError(ACC_7001_ALERT_FORMAT_INVALID, "告警必须是 JSON 对象")
    return alert


def _fmt_time(epoch: float | None) -> str:
    """epoch 秒转本地可读时间。

    Args:
        epoch: 时间戳；None 显示 "-"。

    Returns:
        "YYYY-MM-DD HH:MM:SS" 或 "-"。
    """
    if epoch is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _cmd_submit(args: argparse.Namespace, comp: CliComponents) -> int:
    """submit 子命令：读告警 → 前台同步跑图 → 渲染终态摘要。

    Args:
        args: 命令行参数。
        comp: CLI 组件。

    Returns:
        退出码（closed=0，其余终态=1）。
    """
    alert = _load_alert(args)
    task_id = f"task-{uuid4().hex[:12]}"
    graph = build_diagnosis_graph(
        comp.llm, comp.registry, comp.audit, comp.recovery, cfg=comp.cfg
    )
    final = run_task(
        graph, comp.audit, task_id=task_id, alert_raw=alert, cfg=comp.cfg
    )

    print(f"任务已提交: {task_id}")
    print(f"终态: {final['status']}")
    if final.get("error"):
        err = final["error"]
        print(f"原因: [{err['code']}] {err['message']}")
    if final.get("root_cause"):
        print(f"根因: {final['root_cause']['desc']}（confidence={final['root_cause'].get('confidence')}）")
    if final.get("report"):
        print(f"报告: {final['report']['text']}")
    return 0 if final["status"] == "closed" else 1


def _cmd_status(args: argparse.Namespace, comp: CliComponents) -> int:
    """status 子命令：无参列最近任务；有参渲染单任务详情。

    Args:
        args: 命令行参数。
        comp: CLI 组件。

    Returns:
        退出码（查询不存在=2）。
    """
    task_id = getattr(args, "task_id", None)
    if task_id:
        task = comp.audit.get_task(task_id)
        if task is None:
            raise OpsAgentError(
                GOV_5002_TASK_NOT_FOUND, f"任务不存在: {task_id}", {"task_id": task_id}
            )
        print(f"task_id:    {task['task_id']}")
        print(f"status:     {task['status']}")
        print(f"alert_id:   {task.get('alert_id') or '-'}")
        print(f"scenario:   {task.get('scenario_id') or '-'}")
        print(f"created:    {_fmt_time(task['created_at'])}")
        print(f"updated:    {_fmt_time(task['updated_at'])}")
        print(f"closed:     {_fmt_time(task.get('closed_at'))}")
        if task.get("final_report"):
            print("final_report:")
            print(json.dumps(task["final_report"], ensure_ascii=False, indent=2))
        return 0

    tasks = comp.audit.list_tasks()
    if not tasks:
        print("暂无任务")
        return 0
    print(f"{'TASK_ID':<20} {'STATUS':<14} {'SCENARIO':<14} CREATED")
    for t in tasks:
        scenario = t.get("scenario_id") or "-"
        print(
            f"{t['task_id']:<20} {t['status']:<14} {scenario:<14} {_fmt_time(t['created_at'])}"
        )
    return 0


def _render_timeline(timeline: dict[str, Any]) -> str:
    """跨表合并渲染审计时间线（三表按 (created_at, id) 升序逐行输出）。

    Args:
        timeline: export_timeline 输出。

    Returns:
        人类可读时间线文本。
    """
    task = timeline["task"]
    t0 = task["created_at"]
    lines = [
        f"=== {task['task_id']} 审计时间线 ===",
        f"status={task['status']}  scenario={task.get('scenario_id') or '-'}"
        f"  created={_fmt_time(t0)}  closed={_fmt_time(task.get('closed_at'))}",
    ]
    events: list[tuple[float, int, str]] = []
    for row in timeline["node_runs"]:
        events.append(
            (
                row["created_at"],
                row["id"],
                f"[节点] {row['node_name']} round={row['round_no']} 耗时={row['elapsed_ms']}ms",
            )
        )
    for row in timeline["llm_calls"]:
        suffix = f" 错误={row['error_code']}" if row.get("error_code") else ""
        events.append(
            (
                row["created_at"],
                row["id"],
                f"[模型] {row['node_name']} tier={row['tier']} model={row['model']}"
                f" tokens={row['prompt_tokens']}+{row['completion_tokens']}"
                f" 耗时={row['elapsed_ms']}ms{suffix}",
            )
        )
    for row in timeline["tool_calls"]:
        ok = "成功" if row["success"] else "失败"
        suffix = f" error={row['error_code']}" if row.get("error_code") else ""
        events.append(
            (
                row["created_at"],
                row["id"],
                f"[工具] {row['node_name']}/{row['tool_name']} {ok}"
                f" 耗时={row['elapsed_ms']}ms{suffix}",
            )
        )
    events.sort(key=lambda e: (e[0], e[1]))
    for created_at, _row_id, text in events:
        lines.append(f"  +{created_at - t0:8.2f}s {text}")
    report = task.get("final_report")
    if isinstance(report, dict) and report.get("text"):
        lines.append(f"[报告] {report['text']}")
    return "\n".join(lines)


def _cmd_audit(args: argparse.Namespace, comp: CliComponents) -> int:
    """audit 子命令：导出任务完整审计时间线（文本渲染或 --json 原始数据）。

    Args:
        args: 命令行参数。
        comp: CLI 组件。

    Returns:
        退出码（查询不存在=2，由 GOV-5002 统一映射）。
    """
    timeline = comp.audit.export_timeline(args.task_id)
    if getattr(args, "json_flag", False):
        print(json.dumps(timeline, ensure_ascii=False, indent=2, default=str))
        return 0
    print(_render_timeline(timeline))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。

    Returns:
        含三个子命令的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        prog="opsagent", description="OpsAgent — IT 运维工单智能处理 Agent 系统"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="提交告警并同步执行诊断")
    p_submit.add_argument("alert_file", nargs="?", help="告警 JSON 文件路径")
    p_submit.add_argument("--inline", help="内联告警 JSON 字符串（与文件二选一）")
    p_submit.set_defaults(func=_cmd_submit)

    p_status = sub.add_parser("status", help="查询任务状态（无参列最近任务）")
    p_status.add_argument("task_id", nargs="?", help="任务 ID")
    p_status.set_defaults(func=_cmd_status)

    p_audit = sub.add_parser("audit", help="导出任务审计时间线")
    p_audit.add_argument("task_id", help="任务 ID")
    p_audit.add_argument("--json", dest="json_flag", action="store_true", help="输出原始 JSON")
    p_audit.set_defaults(func=_cmd_audit)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    components: CliComponents | None = None,
) -> int:
    """CLI 主入口（console_scripts 调用；返回值即进程退出码）。

    Args:
        argv: 命令行参数；None 取 sys.argv。
        components: 注入组件（测试替身）；None 时按配置装配。

    Returns:
        退出码：0 成功闭环；1 非闭环终态；2 参数/输入/查询错误。
    """
    args = build_parser().parse_args(argv)
    try:
        comp = components or build_components()
        handler = args.func
        return handler(args, comp)
    except OpsAgentError as exc:
        print(f"错误 [{exc.code}] {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
