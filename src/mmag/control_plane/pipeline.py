"""Durable partitioned processing with an independent delivery worker."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .lifecycle import LifecycleService
from .models import EntityType, InboundEvent, OutboundMessage

if TYPE_CHECKING:
    from .store import SQLiteControlPlane

Processor = Callable[[InboundEvent], Awaitable[tuple[OutboundMessage, ...]]]
Deliverer = Callable[[OutboundMessage], Awaitable[str]]


class PartitionedScheduler:
    """FIFO per conversation, bounded concurrency across conversations."""

    def __init__(
        self,
        handler: Callable[[InboundEvent], Awaitable[None]],
        max_concurrency: int,
        max_pending: int = 256,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._handler = handler
        self._limit = asyncio.Semaphore(max_concurrency)
        self._pending = asyncio.Semaphore(max_pending)
        self._queues: dict[str, asyncio.Queue[InboundEvent | None]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def submit(self, event: InboundEvent) -> None:
        if self._closed:
            raise RuntimeError("scheduler is closed")
        await self._pending.acquire()
        queue = self._queues.get(event.conversation_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[event.conversation_id] = queue
            self._tasks[event.conversation_id] = asyncio.create_task(
                self._worker(queue), name=f"conversation:{event.conversation_id}"
            )
        await queue.put(event)

    async def join(self) -> None:
        await asyncio.gather(*(queue.join() for queue in tuple(self._queues.values())))

    async def close(self) -> None:
        if self._closed:
            return
        await self.join()
        self._closed = True
        for queue in self._queues.values():
            await queue.put(None)
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _worker(self, queue: asyncio.Queue[InboundEvent | None]) -> None:
        while True:
            event = await queue.get()
            try:
                if event is None:
                    return
                async with self._limit:
                    await self._handler(event)
            finally:
                if event is not None:
                    self._pending.release()
                queue.task_done()


class MessagePipeline:
    def __init__(
        self,
        store: SQLiteControlPlane,
        processor: Processor,
        deliverer: Deliverer,
        *,
        max_concurrency: int = 8,
        max_pending: int = 256,
        max_delivery_attempts: int = 3,
        delivery_retry_base_seconds: float = 0.25,
    ):
        self.store = store
        self.lifecycle = LifecycleService(store)
        self.processor = processor
        self.deliverer = deliverer
        self.max_delivery_attempts = max_delivery_attempts
        self.delivery_retry_base_seconds = delivery_retry_base_seconds
        self.scheduler = PartitionedScheduler(self._process, max_concurrency, max_pending)
        self._delivery_task: asyncio.Task[None] | None = None
        self._delivery_wake = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        self.store.recover_deliveries()
        self.lifecycle.reconcile()
        self._delivery_task = asyncio.create_task(self._delivery_loop(), name="delivery-worker")
        for record in self.store.recover_inbox():
            await self.scheduler.submit(record.event)
        self._delivery_wake.set()

    async def accept(self, event: InboundEvent) -> bool:
        if not self.store.accept_event(event):
            return False
        self._ensure_execution_entities(event)
        await self.scheduler.submit(event)
        return True

    async def join(self) -> None:
        await self.scheduler.join()
        self._delivery_wake.set()
        while (
            self.store.list_deliveries(status="pending")
            or self.store.list_deliveries(status="retrying")
            or self.store.list_deliveries(status="sending")
        ):
            await asyncio.sleep(0.005)

    async def close(self) -> None:
        await self.join()
        await self.scheduler.close()
        self._stopping = True
        self._delivery_wake.set()
        if self._delivery_task:
            await self._delivery_task

    async def _process(self, event: InboundEvent) -> None:
        self._ensure_execution_entities(event)
        self.store.mark_inbox_processing(event.event_id)
        self._start_execution(event)
        try:
            messages = await self.processor(event)
            self.store.complete_event(event.event_id, messages)
            self.lifecycle.transition(
                EntityType.AGENT_RUN,
                f"run:{event.event_id}",
                "succeeded",
                command_id=f"run-success:{event.event_id}",
            )
            self.lifecycle.transition(
                EntityType.TASK,
                f"task:{event.event_id}",
                "succeeded",
                command_id=f"task-success:{event.event_id}",
            )
            self._delivery_wake.set()
        except Exception as error:
            self.store.mark_inbox_failed(event.event_id, str(error))
            self._fail_execution(event, str(error))

    async def _delivery_loop(self) -> None:
        while True:
            delivery = self.store.claim_delivery()
            if delivery is None:
                if self._stopping:
                    return
                self._delivery_wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._delivery_wake.wait(), timeout=0.1)
                continue
            try:
                self.lifecycle.transition(
                    EntityType.DELIVERY,
                    delivery.id,
                    "sending",
                    command_id=f"delivery-send:{delivery.id}:{delivery.attempts}",
                )
                remote_id = await self.deliverer(delivery.message)
                if not remote_id:
                    raise RuntimeError("delivery returned no remote id")
                self.store.mark_delivery_delivered(delivery.id, remote_id)
                self.lifecycle.transition(
                    EntityType.DELIVERY,
                    delivery.id,
                    "delivered",
                    command_id=f"delivery-success:{delivery.id}",
                )
            except Exception as error:
                if delivery.attempts >= self.max_delivery_attempts:
                    self.store.mark_delivery_failed(delivery.id, str(error))
                    self.lifecycle.transition(
                        EntityType.DELIVERY,
                        delivery.id,
                        "failed",
                        command_id=f"delivery-failed:{delivery.id}",
                    )
                    continue
                delay = self.delivery_retry_base_seconds * 2 ** (delivery.attempts - 1)
                self.store.mark_delivery_retry(delivery.id, str(error), time.time() + delay)
                self.lifecycle.transition(
                    EntityType.DELIVERY,
                    delivery.id,
                    "retrying",
                    command_id=f"delivery-retry:{delivery.id}:{delivery.attempts}",
                )

    def _ensure_execution_entities(self, event: InboundEvent) -> None:
        for entity_type, prefix in (
            (EntityType.TASK, "task"),
            (EntityType.AGENT_RUN, "run"),
        ):
            entity_id = f"{prefix}:{event.event_id}"
            try:
                self.store.get_lifecycle_entity(entity_type, entity_id)
            except KeyError:
                self.lifecycle.create(
                    entity_type,
                    entity_id,
                    scope_id=event.conversation_id,
                    payload={"inbox_event_id": event.event_id},
                )

    def _fail_execution(self, event: InboundEvent, reason: str) -> None:
        for entity_type, prefix in (
            (EntityType.AGENT_RUN, "run"),
            (EntityType.TASK, "task"),
        ):
            entity = self.store.get_lifecycle_entity(entity_type, f"{prefix}:{event.event_id}")
            if entity.state == "running":
                self.lifecycle.transition(
                    entity_type,
                    entity.entity_id,
                    "failed",
                    command_id=f"{prefix}-failed:{event.event_id}",
                    reason=reason,
                )

    def _start_execution(self, event: InboundEvent) -> None:
        for entity_type, prefix in (
            (EntityType.TASK, "task"),
            (EntityType.AGENT_RUN, "run"),
        ):
            entity_id = f"{prefix}:{event.event_id}"
            entity = self.store.get_lifecycle_entity(entity_type, entity_id)
            self.lifecycle.transition(
                entity_type,
                entity_id,
                "running",
                command_id=f"{prefix}-start:{event.event_id}:v{entity.version}",
                expected_version=entity.version,
            )
