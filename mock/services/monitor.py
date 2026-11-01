"""mock-monitor（8101）：指标/服务状态查询 + 恢复判定 + 故障注入与修复控制端点。

评测执行器与 P1 工作流的修复动作均经本服务 /scenario/fix 落到场景引擎；
/recovery_check 提供客观恢复判定（生产语义：监控系统 SLO API）。
独立进程启动：python -m mock.services.monitor
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from mock.scenario_engine.engine import ScenarioEngine
from mock.services.common import build_engine, register_error_handler
from mock.services.ports import HOST, MONITOR_PORT


class InjectRequest(BaseModel):
    """故障注入请求体。

    Attributes:
        scenario_id: 场景 ID。
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str


class FixRequest(BaseModel):
    """修复动作请求体。

    Attributes:
        host_id: 主机 ID。
        action: 修复动作名（如 restart_service）。
        params: 动作参数（可携带 request_id 等额外键）。
    """

    model_config = ConfigDict(extra="forbid")

    host_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


def create_app(engine: ScenarioEngine | None = None) -> FastAPI:
    """构建 mock-monitor 应用（引擎可注入，测试隔离用）。

    Args:
        engine: 场景引擎；None 时构建默认实例（共享 SQLite 状态库）。

    Returns:
        FastAPI 应用。
    """
    eng = engine or build_engine()
    app = FastAPI(title="mock-monitor")
    register_error_handler(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        """健康探针。

        Returns:
            服务标识。
        """
        return {"status": "ok", "service": "monitor"}

    @app.get("/metrics")
    def metrics(
        host_id: str,
        metric: str,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """查询指标时间序列。

        Args:
            host_id: 主机 ID。
            metric: 指标名。
            start_ts: 窗口起点（缺省回看一个基线周期）。
            end_ts: 窗口终点（缺省 now）。

        Returns:
            {host_id, metric, points}。
        """
        points = eng.get_metrics(host_id, metric, start_ts, end_ts)
        return {"host_id": host_id, "metric": metric, "points": points}

    @app.get("/service_status")
    def service_status(host_id: str) -> dict[str, str]:
        """查询服务运行状态。

        Args:
            host_id: 主机 ID。

        Returns:
            {host_id, status}。
        """
        return {"host_id": host_id, "status": eng.get_service_status(host_id)}

    @app.get("/recovery_check")
    def recovery_check(host_id: str) -> dict[str, Any]:
        """客观恢复判定（DESIGN §5.5.2：引擎判定，生产语义为监控系统 SLO/告警恢复 API）。

        Args:
            host_id: 主机 ID。

        Returns:
            {host_id, recovered, checks, evaluated_at}。
        """
        verdict = eng.evaluate_recovery(host_id)
        return {"host_id": host_id, **verdict}

    @app.post("/scenario/inject")
    def scenario_inject(req: InjectRequest) -> dict[str, Any]:
        """注入故障场景（评测与联调控制面）。

        Args:
            req: 注入请求。

        Returns:
            注入结果摘要。
        """
        return eng.inject_fault(req.scenario_id)

    @app.post("/scenario/fix")
    def scenario_fix(req: FixRequest) -> dict[str, Any]:
        """应用修复动作（工作流执行修复走此端点）。

        Args:
            req: 修复请求。

        Returns:
            {matched, effect, state, at, reason} 摘要。
        """
        return eng.apply_fix(req.host_id, req.action, req.params)

    @app.post("/scenario/reset")
    def scenario_reset() -> dict[str, str]:
        """复位全部模拟状态。

        Returns:
            复位确认。
        """
        eng.reset()
        return {"status": "reset"}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=MONITOR_PORT)
