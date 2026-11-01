"""config.py 单元测试：默认值、yaml 覆盖、环境变量优先级、密钥脱敏。"""

from __future__ import annotations

from pathlib import Path

from opsagent.config import Settings


def test_defaults_without_yaml(tmp_path: Path, monkeypatch) -> None:
    """无配置文件时全部走代码默认值（chdir 隔离避免读到真实 model.yaml）。"""
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.model.provider == "deepseek"
    assert s.model.base_url == "https://api.deepseek.com"
    assert s.model.api_key.get_secret_value() == ""
    assert s.model.model_fast == "deepseek-chat"
    assert s.tools.timeout_seconds == 5.0
    assert s.tools.max_retries == 2
    assert s.workflow.max_rounds == 15
    assert s.workflow.token_budget_default == 8000
    assert s.mock.monitor_port == 8101
    assert s.mock.logs_port == 8102
    assert s.mock.cmdb_port == 8103
    assert s.mock.ticket_port == 8104
    assert s.data_dir == Path("data")


def test_yaml_overrides_defaults(tmp_path: Path, monkeypatch) -> None:
    """yaml 中显式配置的项覆盖默认值，未配置项保持默认。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "model.yaml").write_text(
        "mock:\n  monitor_port: 9101\nworkflow:\n  max_rounds: 3\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.mock.monitor_port == 9101
    assert s.workflow.max_rounds == 3
    assert s.mock.logs_port == 8102  # 未覆盖项不受影响


def test_env_overrides_yaml(tmp_path: Path, monkeypatch) -> None:
    """环境变量优先级高于 yaml（OPSAGENT_ 前缀 + __ 嵌套）。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "model.yaml").write_text("mock:\n  monitor_port: 9101\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPSAGENT_MOCK__MONITOR_PORT", "9201")
    s = Settings()
    assert s.mock.monitor_port == 9201


def test_api_key_not_leaked_by_repr(tmp_path: Path, monkeypatch) -> None:
    """SecretStr 保证 repr/str 不泄漏密钥明文。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPSAGENT_MODEL__API_KEY", "sk-secret-123")
    s = Settings()
    assert s.model.api_key.get_secret_value() == "sk-secret-123"
    assert "sk-secret-123" not in repr(s)
    assert "sk-secret-123" not in str(s)
