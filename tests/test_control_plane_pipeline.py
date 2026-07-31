import asyncio
from collections import defaultdict

import pytest

from mmag.control_plane import (
    InboundEvent,
    MessagePipeline,
    OutboundMessage,
    SQLiteControlPlane,
)


def _event(event_id: str, conversation_id: str) -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        platform="mattermost",
        event_type="posted",
        conversation_id=conversation_id,
        actor_id="user-1",
        occurred_at=1.0,
        payload={"id": event_id},
    )


@pytest.mark.asyncio
async def test_pipeline_preserves_conversation_order_and_runs_conversations_concurrently(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    observed: dict[str, list[str]] = defaultdict(list)
    both_started = asyncio.Event()
    started: set[str] = set()

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        started.add(event.conversation_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        observed[event.conversation_id].append(event.event_id)
        return (OutboundMessage(event.conversation_id, event.event_id),)

    delivered: list[str] = []

    async def deliver(message: OutboundMessage) -> str:
        delivered.append(message.text)
        return f"post-{message.text}"

    pipeline = MessagePipeline(store, process, deliver, max_concurrency=2)
    await pipeline.start()
    assert await pipeline.accept(_event("a1", "a"))
    assert await pipeline.accept(_event("b1", "b"))
    assert await pipeline.accept(_event("a2", "a"))
    assert not await pipeline.accept(_event("a1", "a"))
    await pipeline.join()
    await pipeline.close()

    assert observed["a"] == ["a1", "a2"]
    assert observed["b"] == ["b1"]
    assert sorted(delivered) == ["a1", "a2", "b1"]
    store.close()


@pytest.mark.asyncio
async def test_delivery_retry_does_not_reprocess_agent(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    runs = 0
    attempts = 0

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        nonlocal runs
        runs += 1
        return (OutboundMessage(event.conversation_id, "done"),)

    async def deliver(message: OutboundMessage) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary")
        return "post-1"

    pipeline = MessagePipeline(
        store,
        process,
        deliver,
        delivery_retry_base_seconds=0,
    )
    await pipeline.start()
    await pipeline.accept(_event("one", "a"))
    await pipeline.join()
    await pipeline.close()

    assert runs == 1
    assert attempts == 2
    assert store.get_inbox("one").status == "completed"
    assert store.list_deliveries(status="delivered")[0].remote_id == "post-1"
    store.close()
