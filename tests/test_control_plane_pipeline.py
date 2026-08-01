import asyncio
from collections import defaultdict

import pytest

from mmag.control_plane import (
    EntityType,
    InboundEvent,
    LifecycleService,
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
    idempotency_keys: list[str] = []

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        nonlocal runs
        runs += 1
        return (OutboundMessage(event.conversation_id, "done"),)

    async def deliver(message: OutboundMessage) -> str:
        nonlocal attempts
        attempts += 1
        idempotency_keys.append(message.idempotency_key)
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
    assert idempotency_keys[0]
    assert len(set(idempotency_keys)) == 1
    assert store.get_inbox("one").status == "completed"
    assert store.list_deliveries(status="delivered")[0].remote_id == "post-1"
    store.close()


@pytest.mark.asyncio
async def test_delivery_terminal_state_settles_parent_task(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        return (OutboundMessage(event.conversation_id, "done"),)

    async def fail_delivery(message: OutboundMessage) -> str:
        raise RuntimeError("permanent failure")

    pipeline = MessagePipeline(store, process, fail_delivery, max_delivery_attempts=1)
    await pipeline.start()
    await pipeline.accept(_event("failed-delivery", "channel"))
    await pipeline.join()
    await pipeline.close()

    run = store.get_lifecycle_entity(EntityType.AGENT_RUN, "run:failed-delivery")
    task = store.get_lifecycle_entity(EntityType.TASK, "task:failed-delivery")
    assert run.state == "succeeded"
    assert task.state == "failed"
    store.close()


@pytest.mark.asyncio
async def test_pipeline_retries_transient_processing_failure_with_persisted_attempts(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    runs = 0

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise TimeoutError("model timeout")
        return (OutboundMessage(event.conversation_id, "recovered"),)

    async def deliver(message: OutboundMessage) -> str:
        return "post-1"

    pipeline = MessagePipeline(
        store,
        process,
        deliver,
        max_processing_attempts=2,
        processing_retry_base_seconds=0,
    )
    await pipeline.start()
    await pipeline.accept(_event("retry-process", "channel"))
    await pipeline.join()
    await pipeline.close()

    record = store.get_inbox("retry-process")
    assert runs == 2
    assert record.status == "completed"
    assert record.attempts == 2
    store.close()


@pytest.mark.asyncio
async def test_failed_inbox_is_queryable_and_replayed_as_a_new_idempotent_event(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        if event.event_id == "dead-letter":
            raise ValueError("invalid source payload")
        return ()

    async def deliver(message: OutboundMessage) -> str:
        return "unused"

    pipeline = MessagePipeline(store, process, deliver)
    await pipeline.start()
    await pipeline.accept(_event("dead-letter", "channel"))
    await pipeline.join()

    dead_letters = store.list_dead_letters(limit=10, conversation_id="channel")
    replayed = await pipeline.replay_dead_letter(
        "dead-letter",
        replay_id="dead-letter:replay:operator-command-1",
        actor_id="operator-1",
    )
    duplicate = await pipeline.replay_dead_letter(
        "dead-letter",
        replay_id="dead-letter:replay:operator-command-1",
        actor_id="operator-1",
    )
    await pipeline.join()
    await pipeline.close()

    replay = store.get_inbox("dead-letter:replay:operator-command-1")
    assert [record.event.event_id for record in dead_letters] == ["dead-letter"]
    assert replayed is True
    assert duplicate is False
    assert store.get_inbox("dead-letter").status == "failed"
    assert replay.status == "completed"
    assert replay.event.payload["_mmag_replay"]["source_event_id"] == "dead-letter"
    assert replay.event.payload["_mmag_replay"]["requested_by"] == "operator-1"
    store.close()


@pytest.mark.asyncio
async def test_pipeline_preserves_waiting_approval_lifecycle(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)

    async def process(event: InboundEvent) -> tuple[OutboundMessage, ...]:
        for entity_type, prefix in (
            (EntityType.AGENT_RUN, "run"),
            (EntityType.TASK, "task"),
        ):
            lifecycle.transition(
                entity_type,
                f"{prefix}:{event.event_id}",
                "waiting_approval",
                command_id=f"pause:{prefix}:{event.event_id}",
            )
        return (OutboundMessage(event.conversation_id, "approval needed"),)

    async def deliver(message: OutboundMessage) -> str:
        return "approval-post"

    pipeline = MessagePipeline(store, process, deliver)
    await pipeline.start()
    await pipeline.accept(_event("paused", "channel"))
    await pipeline.join()
    await pipeline.close()

    assert store.get_lifecycle_entity(EntityType.AGENT_RUN, "run:paused").state == (
        "waiting_approval"
    )
    assert store.get_lifecycle_entity(EntityType.TASK, "task:paused").state == "waiting_approval"
    store.close()
