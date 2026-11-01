"""mock-cmdb（8103）：主机资产档案查询。

独立进程启动：python -m mock.services.cmdb
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from mock.scenario_engine.engine import ScenarioEngine
from mock.scenario_engine.errors import MOCK_4006_HOST_NOT_FOUND, ScenarioError
from mock.services.common import build_engine, register_error_handler
from mock.services.ports import CMDB_PORT, HOST


def create_app(engine: ScenarioEngine | None = None) -> FastAPI:
    """构建 mock-cmdb 应用（引擎可注入，测试隔离用）。

    Args:
        engine: 场景引擎；None 时构建默认实例。

    Returns:
        FastAPI 应用。
    """
    eng = engine or build_engine()
    app = FastAPI(title="mock-cmdb")
    register_error_handler(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        """健康探针。

        Returns:
            服务标识。
        """
        return {"status": "ok", "service": "cmdb"}

    @app.get("/host")
    def host_info(host_id: str) -> dict[str, Any]:
        """查询主机资产档案。

        Args:
            host_id: 主机 ID。

        Returns:
            资产字典（含 ip/os/service/owner 等字段）。

        Raises:
            ScenarioError: 查无此主机（MOCK-4006 → HTTP 404）。
        """
        asset = eng.get_cmdb(host_id)
        if asset is None:
            raise ScenarioError(MOCK_4006_HOST_NOT_FOUND, f"主机不存在: {host_id}")
        return asset

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=CMDB_PORT)
