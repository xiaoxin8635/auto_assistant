"""errors.py 单元测试：错误码格式校验、属性、序列化。"""

from __future__ import annotations

import pytest

from opsagent.errors import (
    ACC_7001_ALERT_FORMAT_INVALID,
    INF_8001_DB_OPERATION_FAILED,
    OpsAgentError,
)


def test_valid_error_attributes() -> None:
    """合法错误码：属性赋值与序列化 round-trip。"""
    err = OpsAgentError(ACC_7001_ALERT_FORMAT_INVALID, "告警格式非法", {"field": "host"})
    assert err.code == "ACC-7001"
    assert err.message == "告警格式非法"
    assert err.detail == {"field": "host"}
    assert err.to_dict() == {
        "code": "ACC-7001",
        "message": "告警格式非法",
        "detail": {"field": "host"},
    }
    assert str(err) == "告警格式非法"


def test_detail_defaults_to_empty_dict() -> None:
    """省略 detail 时应为空字典而非 None。"""
    err = OpsAgentError(INF_8001_DB_OPERATION_FAILED, "数据库操作失败")
    assert err.detail == {}


@pytest.mark.parametrize("bad_code", ["bad-code", "TOOL-123", "TOOL-12345", "tool-1001", ""])
def test_invalid_code_rejected(bad_code: str) -> None:
    """错误码不符合 XXX-1234 格式时必须在构造期直接抛 ValueError。"""
    with pytest.raises(ValueError):
        OpsAgentError(bad_code, "message")


def test_variable_length_sections_accepted() -> None:
    """模块段 2~4 字母均合法（回归：TOOL 4 字母 / WF 2 字母曾被 3 字母正则误拒）。"""
    assert OpsAgentError("TOOL-1001", "工具调用失败").code == "TOOL-1001"
    assert OpsAgentError("WF-3001", "工作流错误").code == "WF-3001"
    assert OpsAgentError("MOCK-4006", "主机不存在").code == "MOCK-4006"
