"""mock-ticket（8104）：模拟工单系统的创建/查询/闭环。

独立进程启动：python -m mock.services.ticket
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from mock.scenario_engine.errors import MOCK_4006_HOST_NOT_FOUND, ScenarioError
from mock.scenario_engine.state_store import MockStateStore
from mock.services.common import build_store, register_error_handler
from mock.services.ports import HOST, TICKET_PORT


class TicketCreateRequest(BaseModel):
    """工单创建请求体。

    Attributes:
        title: 工单标题。
        content: 工单描述。
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    content: str = Field(default="")


def create_app(store: MockStateStore | None = None) -> FastAPI:
    """构建 mock-ticket 应用（状态库可注入，测试隔离用）。

    Args:
        store: 共享状态库；None 时构建默认实例。

    Returns:
        FastAPI 应用。
    """
    ticket_store = store or build_store()
    app = FastAPI(title="mock-ticket")
    register_error_handler(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        """健康探针。

        Returns:
            服务标识。
        """
        return {"status": "ok", "service": "ticket"}

    @app.post("/tickets", status_code=201)
    def create_ticket(req: TicketCreateRequest) -> dict[str, Any]:
        """创建工单。

        Args:
            req: 创建请求。

        Returns:
            工单字典。
        """
        return ticket_store.create_ticket(req.title, req.content)

    @app.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: str) -> dict[str, Any]:
        """查询工单。

        Args:
            ticket_id: 工单 ID。

        Returns:
            工单字典。

        Raises:
            ScenarioError: 工单不存在（MOCK-4006 → HTTP 404）。
        """
        ticket = ticket_store.get_ticket(ticket_id)
        if ticket is None:
            raise ScenarioError(MOCK_4006_HOST_NOT_FOUND, f"工单不存在: {ticket_id}")
        return ticket

    @app.post("/tickets/{ticket_id}/close")
    def close_ticket(ticket_id: str) -> dict[str, Any]:
        """闭环工单。

        Args:
            ticket_id: 工单 ID。

        Returns:
            {ticket_id, closed}。

        Raises:
            ScenarioError: 工单不存在（MOCK-4006 → HTTP 404）。
        """
        if not ticket_store.close_ticket(ticket_id):
            raise ScenarioError(MOCK_4006_HOST_NOT_FOUND, f"工单不存在: {ticket_id}")
        return {"ticket_id": ticket_id, "closed": True}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=TICKET_PORT)
