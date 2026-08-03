"""配置日志不得泄露 Secret 的任何片段。"""

from importlib import import_module

import pytest

from mmag.application.app import _validate_database_paths
from mmag.config import Config, _log_config_loading, _text_env


def test_config_log_reports_secret_presence_without_value(monkeypatch):
    monkeypatch.setenv("MM_TOKEN", "MM_PREFIX_123456_MM_SUFFIX")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "AK_PREFIX_123456_AK_SUFFIX")
    monkeypatch.setenv("MM_SLASH_COMMAND_TOKEN", "SC_PREFIX_123456_SC_SUFFIX")
    captured: dict = {}

    def capture(_logger, event: str, **fields):
        captured.update(event=event, **fields)

    config_module = import_module("mmag.config")
    monkeypatch.setattr(config_module, "log_event", capture)

    _log_config_loading()

    assert captured["event"] == "config.loaded"
    assert "MM_TOKEN" in captured["configured_fields"]
    assert "ANTHROPIC_API_KEY" in captured["configured_fields"]
    assert "MM_SLASH_COMMAND_TOKEN" in captured["configured_fields"]
    rendered = repr(captured)
    assert "MM_PREFIX" not in rendered
    assert "AK_PREFIX" not in rendered
    assert "SC_PREFIX" not in rendered


def test_config_repr_does_not_expose_field_values():
    assert repr(Config()).startswith("<mmag.config.Config object at ")


def test_business_and_checkpoint_databases_must_be_separate(tmp_path):
    business = tmp_path / "business.db"
    checkpoint = tmp_path / "checkpoints.db"

    _validate_database_paths(str(business), str(checkpoint))
    with pytest.raises(ValueError, match="must use separate files"):
        _validate_database_paths(str(business), str(tmp_path / "." / "business.db"))


def test_empty_ack_override_keeps_code_default(monkeypatch):
    monkeypatch.setenv("MM_ACK_MESSAGE", "")
    assert _text_env("MM_ACK_MESSAGE", "get") == "get"

    monkeypatch.setenv("MM_ACK_MESSAGE", "已收到")
    assert _text_env("MM_ACK_MESSAGE", "get") == "已收到"
