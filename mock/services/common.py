"""mock 服务公共装配：引擎/状态库构建、ScenarioError → HTTP 错误体映射。

错误响应统一结构 {"code", "message", "detail"}，与业务侧 OpsAgentError.to_dict 对齐。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mock.scenario_engine.engine import ScenarioEngine
from mock.scenario_engine.errors import (
    MOCK_4001_SCENARIO_NOT_FOUND,
    MOCK_4004_STATE_INVALID,
    MOCK_4005_DATA_INVALID,
    MOCK_4006_HOST_NOT_FOUND,
    MOCK_4007_STATE_STORE_ERROR,
    ScenarioError,
)
from mock.scenario_engine.state_store import DEFAULT_DB_PATH, MockStateStore

# 错误码 → HTTP 状态码映射（对齐 REST 语义：不存在 404 / 状态冲突 409 / 数据非法 422 / 内部 500）
STATUS_MAP: dict[str, int] = {
    MOCK_4001_SCENARIO_NOT_FOUND: 404,
    MOCK_4004_STATE_INVALID: 409,
    MOCK_4005_DATA_INVALID: 422,
    MOCK_4006_HOST_NOT_FOUND: 404,
    MOCK_4007_STATE_STORE_ERROR: 500,
}


def register_error_handler(app: FastAPI) -> None:
    """注册 ScenarioError 全局异常处理器（统一错误体）。

    Args:
        app: FastAPI 应用。
    """

    @app.exception_handler(ScenarioError)
    async def _scenario_error_handler(request: Any, exc: ScenarioError) -> JSONResponse:  # noqa: ARG001
        """把 ScenarioError 映射为 HTTP 响应。

        Args:
            request: 请求对象（未使用）。
            exc: 场景域异常。

        Returns:
            JSON 错误响应。
        """
        status = STATUS_MAP.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )


def repo_root() -> Path:
    """定位仓库根目录（mock 包固定位于 <root>/mock）。

    Returns:
        仓库根 Path。
    """
    return Path(__file__).resolve().parents[2]


def build_store() -> MockStateStore:
    """构建共享状态库（路径可经环境变量 MOCK_DB_PATH 覆盖，测试隔离用）并建表。

    Returns:
        已初始化 schema 的状态库。
    """
    db_path = Path(os.environ.get("MOCK_DB_PATH", str(DEFAULT_DB_PATH)))
    store = MockStateStore(db_path)
    store.init_schema()
    return store


def build_engine(clock: Callable[[], float] | None = None) -> ScenarioEngine:
    """构建场景引擎（场景/资产固定取仓库 mock/scenarios 目录）。

    Args:
        clock: 可注入时钟（缺省真实时间）。

    Returns:
        绑定共享状态库的场景引擎。
    """
    scenarios_dir = repo_root() / "mock" / "scenarios"
    return ScenarioEngine(scenarios_dir, scenarios_dir / "hosts.yaml", build_store(), clock=clock)


__all__ = ["STATUS_MAP", "build_engine", "build_store", "register_error_handler", "repo_root"]
