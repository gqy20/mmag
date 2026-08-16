import asyncio
from unittest.mock import AsyncMock, MagicMock

from mmag import cli


def test_console_entrypoint_runs_and_stops_agent(monkeypatch):
    agent = MagicMock()
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    monkeypatch.setattr(cli, "Agent", MagicMock(return_value=agent))
    monkeypatch.setattr(cli, "init_logging", MagicMock())

    result = cli.main()

    assert result is None
    agent.start.assert_awaited_once()
    agent.stop.assert_awaited_once()


def test_console_entrypoint_gracefully_stops_after_cancellation(monkeypatch):
    agent = MagicMock()
    agent.start = AsyncMock(side_effect=asyncio.CancelledError)
    agent.stop = AsyncMock()
    monkeypatch.setattr(cli, "Agent", MagicMock(return_value=agent))
    monkeypatch.setattr(cli, "init_logging", MagicMock())

    result = cli.main()

    assert result is None
    agent.stop.assert_awaited_once()
