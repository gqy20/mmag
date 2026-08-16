"""Central lifecycle transition rules shared by every runtime."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal

from .models import EntityType, LifecycleEntity

if TYPE_CHECKING:
    from .store import SQLiteControlPlane


class LifecycleError(RuntimeError):
    pass


class InvalidTransitionError(LifecycleError):
    pass


class VersionConflictError(LifecycleError):
    pass


_INITIAL = {
    EntityType.TASK: "queued",
    EntityType.AGENT_RUN: "queued",
    EntityType.CAPABILITY_CALL: "requested",
    EntityType.APPROVAL_REQUEST: "pending",
    EntityType.DELIVERY: "pending",
}

_TRANSITIONS: dict[EntityType, dict[str, frozenset[str]]] = {
    EntityType.TASK: {
        "queued": frozenset({"running", "cancelled"}),
        "running": frozenset({"waiting_approval", "succeeded", "failed", "cancelled"}),
        "waiting_approval": frozenset({"running", "failed", "cancelled"}),
    },
    EntityType.AGENT_RUN: {
        "queued": frozenset({"running", "cancelled"}),
        "running": frozenset(
            {
                "waiting_child",
                "waiting_approval",
                "succeeded",
                "exhausted",
                "failed",
                "cancelled",
            }
        ),
        "waiting_child": frozenset({"queued", "failed", "cancelled"}),
        "waiting_approval": frozenset({"queued", "failed", "cancelled"}),
    },
    EntityType.CAPABILITY_CALL: {
        "requested": frozenset({"running", "waiting_approval", "rejected", "cancelled"}),
        "running": frozenset({"waiting_approval", "succeeded", "failed", "cancelled"}),
        "waiting_approval": frozenset({"requested", "rejected", "cancelled"}),
    },
    EntityType.APPROVAL_REQUEST: {
        "pending": frozenset({"approved", "rejected", "expired", "cancelled"}),
    },
    EntityType.DELIVERY: {
        "pending": frozenset({"sending", "cancelled"}),
        "sending": frozenset({"delivered", "retrying", "failed"}),
        "retrying": frozenset({"sending", "failed", "cancelled"}),
        "failed": frozenset({"retrying"}),
    },
}

_RECOVERY = {
    (EntityType.TASK, "running"): "queued",
    (EntityType.AGENT_RUN, "running"): "queued",
    (EntityType.CAPABILITY_CALL, "running"): "requested",
    (EntityType.DELIVERY, "sending"): "pending",
}


class LifecycleService:
    """The only supported mutation boundary for durable business state."""

    def __init__(self, store: SQLiteControlPlane):
        self.store = store

    def create(
        self,
        entity_type: EntityType,
        entity_id: str,
        *,
        scope_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> LifecycleEntity:
        return self.store.create_lifecycle_entity(
            entity_type, entity_id, _INITIAL[entity_type], scope_id, payload or {}
        )

    def transition(
        self,
        entity_type: EntityType,
        entity_id: str,
        to_state: str,
        *,
        command_id: str,
        expected_version: int | None = None,
        reason: str = "",
        actor_id: str = "",
        trace_id: str = "",
        recovery: bool = False,
        payload_patch: dict[str, Any] | None = None,
        delivery_error: str | None = None,
        delivery_retry_at: float | None = None,
        delivery_remote_id: str | None = None,
        delivery_attempts: Literal["preserve", "increment", "reset"] = "preserve",
    ) -> LifecycleEntity:
        prior = self.store.find_transition(command_id)
        if prior:
            if prior.entity_type != entity_type or prior.entity_id != entity_id:
                raise LifecycleError(f"command_id {command_id!r} is already used")
            return self.store.get_lifecycle_entity(entity_type, entity_id)

        current = self.store.get_lifecycle_entity(entity_type, entity_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflictError(
                f"expected version {expected_version}, found {current.version}"
            )
        allowed = _TRANSITIONS[entity_type].get(current.state, frozenset())
        if not recovery and to_state not in allowed:
            raise InvalidTransitionError(
                f"invalid {entity_type.value} transition {current.state} -> {to_state}"
            )
        try:
            return self.store.transition_lifecycle(
                current,
                to_state,
                command_id,
                reason,
                actor_id,
                trace_id,
                payload_patch,
                recovery,
                delivery_error,
                delivery_retry_at,
                delivery_remote_id,
                delivery_attempts,
            )
        except RuntimeError as error:
            if str(error) == "version_conflict":
                raise VersionConflictError("concurrent lifecycle update") from error
            raise

    def reconcile(self) -> tuple[LifecycleEntity, ...]:
        recovered: list[LifecycleEntity] = []
        for entity in self.store.list_lifecycle_entities():
            state = _RECOVERY.get((entity.entity_type, entity.state))
            if state is None:
                continue
            recovered.append(
                self.transition(
                    entity.entity_type,
                    entity.entity_id,
                    state,
                    command_id=f"reconcile:{entity.entity_type.value}:{entity.entity_id}:{uuid.uuid4().hex}",
                    reason="process restarted before terminal state",
                    recovery=True,
                )
            )
        return tuple(recovered)
