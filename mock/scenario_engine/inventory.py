"""静态资产库（CMDB 内存版）：从 mock/scenarios/hosts.yaml 加载主机档案。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mock.scenario_engine.errors import MOCK_4005_DATA_INVALID, ScenarioError


def load_inventory(inventory_path: Path) -> dict[str, dict[str, Any]]:
    """加载主机资产档案。

    Args:
        inventory_path: hosts.yaml 路径（顶层键 `hosts`，列表元素含 host_id 等字段）。

    Returns:
        host_id -> 资产字典 的映射。

    Raises:
        ScenarioError: 文件缺失、顶层结构非法或存在重复 host_id（MOCK-4005）。
    """
    if not inventory_path.exists():
        raise ScenarioError(
            MOCK_4005_DATA_INVALID, f"资产档案不存在: {inventory_path}"
        )
    data = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), list):
        raise ScenarioError(
            MOCK_4005_DATA_INVALID, f"资产档案结构非法（缺少顶层 hosts 列表）: {inventory_path}"
        )
    inventory: dict[str, dict[str, Any]] = {}
    for item in data["hosts"]:
        host_id = item.get("host_id")
        if not host_id:
            raise ScenarioError(MOCK_4005_DATA_INVALID, "资产条目缺少 host_id")
        if host_id in inventory:
            raise ScenarioError(MOCK_4005_DATA_INVALID, f"重复 host_id: {host_id}")
        inventory[host_id] = dict(item)
    return inventory
