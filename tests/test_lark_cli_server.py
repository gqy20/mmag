from __future__ import annotations

import pytest

from mmag import lark_cli_server


@pytest.mark.asyncio
async def test_fetch_document_returns_hashed_source(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def run(*arguments: str):
        calls.append(arguments)
        return {"document_id": "doc-1", "revision_id": 7, "content": "private body"}

    monkeypatch.setattr(lark_cli_server, "_run", run)

    result = await lark_cli_server.fetch_document("https://example.feishu.cn/docx/doc-1")

    assert calls == [
        (
            "docs",
            "+fetch",
            "--doc",
            "https://example.feishu.cn/docx/doc-1",
            "--detail",
            "simple",
        )
    ]
    assert result["sources"][0]["resource_id"] == "doc-1"
    assert result["sources"][0]["version"] == "7"
    assert len(result["sources"][0]["content_sha256"]) == 64


@pytest.mark.asyncio
async def test_create_task_requires_confirmed_open_id(monkeypatch) -> None:
    async def run(*arguments: str):
        return {"arguments": arguments}

    monkeypatch.setattr(lark_cli_server, "_run", run)

    with pytest.raises(ValueError, match="open_id"):
        await lark_cli_server.create_task("Ship", assignee_open_id="someone")

    result = await lark_cli_server.create_task(
        "Ship",
        due="2026-08-20T18:00:00+08:00",
        assignee_open_id="ou_confirmed",
        idempotency_key="workflow-1-task-1",
    )
    assert result["arguments"] == (
        "task",
        "+create",
        "--summary",
        "Ship",
        "--due",
        "2026-08-20T18:00:00+08:00",
        "--assignee",
        "ou_confirmed",
        "--idempotency-key",
        "workflow-1-task-1",
    )


@pytest.mark.asyncio
async def test_reminder_uses_fixed_command(monkeypatch) -> None:
    async def run(*arguments: str):
        return {"arguments": arguments}

    monkeypatch.setattr(lark_cli_server, "_run", run)
    result = await lark_cli_server.set_task_reminder("guid-1", "1d")
    assert result["arguments"] == (
        "task",
        "+reminder",
        "--task-id",
        "guid-1",
        "--set",
        "1d",
    )
