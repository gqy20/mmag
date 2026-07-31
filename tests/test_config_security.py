"""配置日志不得泄露 Secret 的任何片段。"""

import logging

from mmag.config import _log_config_loading


def test_config_log_reports_secret_presence_without_value(monkeypatch, caplog):
    monkeypatch.setenv("MM_TOKEN", "MM_PREFIX_123456_MM_SUFFIX")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "AK_PREFIX_123456_AK_SUFFIX")
    caplog.set_level(logging.INFO)

    _log_config_loading()

    assert "MM_TOKEN = (已设置)" in caplog.text
    assert "ANTHROPIC_API_KEY = (已设置)" in caplog.text
    assert "MM_PREFI" not in caplog.text
    assert "AK_PREFI" not in caplog.text
