"""mock-logs（8102）：按主机/服务/关键字检索模拟日志。

独立进程启动：python -m mock.services.logs
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from mock.scenario_engine.engine import ScenarioEngine
from mock.services.common import build_engine, register_error_handler
from mock.services.ports import HOST, LOGS_PORT


def create_app(engine: ScenarioEngine | None = None) -> FastAPI:
    """构建 mock-logs 应用（引擎可注入，测试隔离用）。

    Args:
        engine: 场景引擎；None 时构建默认实例。

    Returns:
        FastAPI 应用。
    """
    eng = engine or build_engine()
    app = FastAPI(title="mock-logs")
    register_error_handler(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        """健康探针。

        Returns:
            服务标识。
        """
        return {"status": "ok", "service": "logs"}

    @app.get("/search")
    def search(
        host_id: str,
        service: str | None = None,
        keyword: str = "",
        lines: int = 20,
    ) -> dict[str, Any]:
        """按主机/服务/关键字检索日志（时间倒序）。

        Args:
            host_id: 主机 ID。
            service: 服务过滤。
            keyword: 正文关键字（大小写不敏感）。
            lines: 最大返回条数。

        Returns:
            {host_id, entries}。
        """
        entries = eng.search_logs(host_id, service, keyword, lines)
        return {"host_id": host_id, "entries": entries}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=LOGS_PORT)
