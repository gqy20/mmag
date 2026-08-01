"""Durable partitioned processing with an independent delivery worker."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..logger import get_logger
from .lifecycle import LifecycleService
from .models import DeliveryRecord, EntityType, InboundEvent, OutboundMessage

if TYPE_CHECKING:
    from .store import SQLiteControlPlane

Processor = Callable[[InboundEvent], Awaitable[tuple[OutboundMessage, ...]]]
Deliverer = Callable[[OutboundMessage], Awaitable[str]]
log = get_logger(__name__)


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
        max_processing_attempts: int = 3,
        processing_retry_base_seconds: float = 0.25,
    ):
        self.store = store
        self.lifecycle = LifecycleService(store)
        self.processor = processor
        self.deliverer = deliverer
        self.max_delivery_attempts = max_delivery_attempts
        self.delivery_retry_base_seconds = delivery_retry_base_seconds
        self.max_processing_attempts = max_processing_attempts
        self.processing_retry_base_seconds = processing_retry_base_seconds
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

    async def replay_dead_letter(
        self,
        event_id: str,
        *,
        replay_id: str,
        actor_id: str,
    ) -> bool:
        """Replay one failed Inbox record as a new, caller-idempotent event."""
        if not replay_id or not actor_id:
            raise ValueError("replay_id and actor_id are required")
        source = self.store.get_inbox(event_id)
        if source.status != "failed":
            raise ValueError(f"inbox event {event_id!r} is not a dead letter")
        try:
            existing = self.store.get_inbox(replay_id)
        except KeyError:
            pass
        else:
            replay_metadata = existing.event.payload.get("_mmag_replay", {})
            if replay_metadata.get("source_event_id") != event_id:
                raise ValueError(f"replay id {replay_id!r} is already used by another event")
            return False

        payload = dict(source.event.payload)
        payload["_mmag_replay"] = {
            "source_event_id": event_id,
            "requested_by": actor_id,
            "source_attempts": source.attempts,
            "source_error": source.last_error,
        }
        replay = InboundEvent(
            event_id=replay_id,
            platform=source.event.platform,
            event_type=source.event.event_type,
            conversation_id=source.event.conversation_id,
            actor_id=source.event.actor_id,
            occurred_at=time.time(),
            payload=payload,
        )
        accepted = await self.accept(replay)
        if accepted:
            self._append_replay_audit(source.event, replay, actor_id)
        return accepted

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
        self._start_execution(event)
        while True:
            self.store.mark_inbox_processing(event.event_id)
            try:
                messages = await self.processor(event)
            except Exception as error:
                record = self.store.get_inbox(event.event_id)
                if self._can_retry_processing(error, record.attempts):
                    delay = self.processing_retry_base_seconds * 2 ** (record.attempts - 1)
                    self.store.mark_inbox_retry(event.event_id, str(error), time.time() + delay)
                    self._append_inbox_audit(event, "retrying", record.attempts, error)
                    await asyncio.sleep(delay)
                    continue
                self.store.mark_inbox_failed(event.event_id, str(error))
                self._append_inbox_audit(event, "failed", record.attempts, error)
                self._fail_execution(event, str(error))
                return
            self._complete_execution(event, messages)
            return

    def _complete_execution(
        self, event: InboundEvent, messages: tuple[OutboundMessage, ...]
    ) -> None:
        self.store.complete_event(event.event_id, messages)
        run_id = f"run:{event.event_id}"
        run = self.store.get_lifecycle_entity(EntityType.AGENT_RUN, run_id)
        failed = any(message.message_kind == "error" for message in messages)
        if run.state == "running":
            self.lifecycle.transition(
                EntityType.AGENT_RUN,
                run_id,
                "failed" if failed else "succeeded",
                command_id=f"run-{'failed' if failed else 'success'}:{event.event_id}",
                reason="agent returned an error response" if failed else "",
            )
        if not messages:
            self._transition_task(event.event_id, "succeeded", "no delivery required")
        self._delivery_wake.set()

    def _can_retry_processing(self, error: Exception, attempts: int) -> bool:
        retryable = isinstance(error, (ConnectionError, TimeoutError)) or bool(
            getattr(error, "retryable", False)
        )
        return retryable and attempts < self.max_processing_attempts

    def _append_inbox_audit(
        self, event: InboundEvent, decision: str, attempt: int, error: Exception
    ) -> None:
        try:
            self.store.append_audit(
                f"inbox.{decision}",
                actor_id=event.actor_id,
                scope_id=event.conversation_id,
                target=event.event_id,
                decision=decision,
                details={"attempt": attempt, "error": str(error)},
            )
        except Exception:
            log.exception("inbox %s audit write failed", event.event_id)

    def _append_replay_audit(
        self,
        source: InboundEvent,
        replay: InboundEvent,
        actor_id: str,
    ) -> None:
        try:
            self.store.append_audit(
                "inbox.replayed",
                actor_id=actor_id,
                scope_id=source.conversation_id,
                target=replay.event_id,
                decision="accepted",
                details={"source_event_id": source.event_id},
            )
        except Exception:
            log.exception("inbox replay %s audit write failed", replay.event_id)

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
            await self._deliver_one(delivery)

    async def _deliver_one(self, delivery: DeliveryRecord) -> None:
        self.lifecycle.transition(
            EntityType.DELIVERY,
            delivery.id,
            "sending",
            command_id=f"delivery-send:{delivery.id}:{delivery.attempts}",
        )
        try:
            remote_id = await self.deliverer(delivery.message)
            if not remote_id:
                raise RuntimeError("delivery returned no remote id")
        except Exception as error:
            self._handle_delivery_failure(delivery, error)
            return
        self.store.mark_delivery_delivered(delivery.id, remote_id)
        self.lifecycle.transition(
            EntityType.DELIVERY,
            delivery.id,
            "delivered",
            command_id=f"delivery-success:{delivery.id}",
        )
        self._settle_parent_task(delivery.message.agent_run_id)
        self._append_delivery_audit(delivery, "delivered", remote_id=remote_id)

    def _handle_delivery_failure(self, delivery: DeliveryRecord, error: Exception) -> None:
        if delivery.attempts >= self.max_delivery_attempts:
            self.store.mark_delivery_failed(delivery.id, str(error))
            self.lifecycle.transition(
                EntityType.DELIVERY,
                delivery.id,
                "failed",
                command_id=f"delivery-failed:{delivery.id}",
            )
            self._settle_parent_task(delivery.message.agent_run_id, failed=True)
            self._append_delivery_audit(delivery, "failed", error=str(error))
            return
        delay = self.delivery_retry_base_seconds * 2 ** (delivery.attempts - 1)
        self.store.mark_delivery_retry(delivery.id, str(error), time.time() + delay)
        self.lifecycle.transition(
            EntityType.DELIVERY,
            delivery.id,
            "retrying",
            command_id=f"delivery-retry:{delivery.id}:{delivery.attempts}",
        )

    def _append_delivery_audit(
        self, delivery: DeliveryRecord, decision: str, **details: str
    ) -> None:
        try:
            self.store.append_audit(
                f"delivery.{decision}",
                scope_id=delivery.message.conversation_id,
                target=delivery.id,
                decision=decision,
                details=details,
            )
        except Exception:
            log.exception("delivery %s audit write failed", delivery.id)

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

    def _settle_parent_task(self, agent_run_id: str, *, failed: bool = False) -> None:
        if not agent_run_id.startswith("run:"):
            return
        event_id = agent_run_id.removeprefix("run:")
        if failed:
            self._transition_task(event_id, "failed", "delivery failed")
            return
        deliveries = self.store.list_deliveries_for_run(agent_run_id)
        if deliveries and all(delivery.status == "delivered" for delivery in deliveries):
            run = self.store.get_lifecycle_entity(EntityType.AGENT_RUN, agent_run_id)
            if run.state == "failed":
                self._transition_task(event_id, "failed", "agent failed; error delivery completed")
            else:
                self._transition_task(event_id, "succeeded", "all deliveries completed")

    def _transition_task(self, event_id: str, target: str, reason: str) -> None:
        task_id = f"task:{event_id}"
        try:
            task = self.store.get_lifecycle_entity(EntityType.TASK, task_id)
        except KeyError:
            return
        if task.state != "running":
            return
        self.lifecycle.transition(
            EntityType.TASK,
            task_id,
            target,
            command_id=f"task-{target}:{event_id}:v{task.version}",
            expected_version=task.version,
            reason=reason,
        )
