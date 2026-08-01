"""Focused contracts for Runtime-to-Mattermost text streaming."""

from unittest.mock import AsyncMock

import pytest

from mmag.application import MattermostStream
from mmag.capabilities import CapabilityRegistry
from mmag.runtimes import (
    LangGraphRuntimeAdapter,
    RunContext,
    RunEvent,
    RunEventKind,
    RunRequest,
)


class _StreamingBackend:
    async def chat_stream(self, messages, *, system, max_tokens, on_text):
        assert messages == [{"role": "user", "content": "hello"}]
        await on_text("Hel")
        await on_text("lo")
        return "Hello"


@pytest.mark.asyncio
async def test_langgraph_text_turn_emits_deltas_and_keeps_final_result():
    events: list[RunEvent] = []

    async def collect(event: RunEvent) -> None:
        events.append(event)

    runtime = LangGraphRuntimeAdapter(
        _StreamingBackend(),  # type: ignore[arg-type]
        capability_registry=CapabilityRegistry(),
    )
    request = RunRequest(
        context=RunContext("trace-1", "user-1", "channel-1", "scope-1"),
        messages=({"role": "user", "content": "hello"},),
        event_sink=collect,
    )

    result = await runtime.run(request)

    assert result.text == "Hello"
    assert [event.text for event in events] == ["Hel", "lo"]
    assert all(event.kind is RunEventKind.TEXT_DELTA for event in events)


@pytest.mark.asyncio
async def test_mattermost_stream_creates_then_updates_one_post_per_round():
    mm = AsyncMock()
    mm.send_post_async.return_value = "stream-1"
    mm.update_post_async.return_value = "stream-1"
    stream = MattermostStream(
        mm,
        {"id": "post-1", "channel_id": "channel-1"},
        "run-1",
        min_interval_seconds=0,
        min_chars=1,
    )

    await stream(RunEvent(RunEventKind.TEXT_DELTA, "Hel", 1))
    await stream(RunEvent(RunEventKind.TEXT_DELTA, "lo", 1))
    await stream(RunEvent(RunEventKind.TEXT_DELTA, "Final", 2))

    mm.send_post_async.assert_awaited_once()
    assert mm.update_post_async.await_count == 2
    final_message = mm.update_post_async.await_args_list[-1].args[1]
    assert "Final" in final_message
    assert "Hello" not in final_message
    assert stream.post_id == "stream-1"
