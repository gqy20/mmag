"""Durable, replay-safe CapabilityCall lifecycle."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .models import (
    CapabilityCallRecord,
    CapabilityCallSpec,
    CapabilityCallState,
    EntityType,
)

if TYPE_CHECKING:
    from .lifecycle import LifecycleService
    from .store import SQLiteControlPlane


class CapabilityCallConflictError(RuntimeError):
    """A call id or execution key was reused with a different trusted identity."""


class CapabilityCallStore:
    def __init__(self, connection: Any, lock: Any, lifecycle_audit: Any) -> None:
        self._connection = connection
        self._lock = lock
        self._lifecycle_audit = lifecycle_audit

    def create_or_get(self, spec: CapabilityCallSpec) -> tuple[CapabilityCallRecord, bool]:
        payload = self._identity_payload(spec)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._find(execution_key=spec.execution_key)
                if row is None:
                    row = self._find(call_id=spec.call_id)
                if row is not None:
                    self._assert_identity(row, payload)
                    self._connection.commit()
                    return self._record(row), False
                self._assert_run(spec)
                now = time.time()
                self._connection.execute(
                    """INSERT INTO lifecycle_entities
                    (entity_type, entity_id, scope_id, state, payload, created_at, updated_at)
                    VALUES ('capability_call', ?, ?, 'requested', ?, ?, ?)""",
                    (spec.call_id, spec.scope_id, self._json(payload), now, now),
                )
                self._lifecycle_audit(
                    entity_type=EntityType.CAPABILITY_CALL,
                    entity_id=spec.call_id,
                    action="created",
                    from_state="",
                    to_state=CapabilityCallState.REQUESTED.value,
                    version=0,
                    scope_id=spec.scope_id,
                    actor_id=spec.actor_id,
                    trace_id=spec.trace_id,
                    payload=payload,
                    created_at=now,
                )
                row = self._find(call_id=spec.call_id)
                if row is None:  # pragma: no cover
                    raise RuntimeError("created CapabilityCall could not be read")
                self._connection.commit()
                return self._record(row), True
            except Exception:
                self._connection.rollback()
                raise

    def get(self, call_id: str) -> CapabilityCallRecord:
        row = self._find(call_id=call_id)
        if row is None:
            raise KeyError(f"capability_call:{call_id}")
        return self._record(row)

    def find_waiting(
        self,
        *,
        run_id: str,
        capability: str,
        input_sha256: str,
    ) -> CapabilityCallRecord | None:
        row = self._connection.execute(
            """SELECT * FROM lifecycle_entities
            WHERE entity_type='capability_call' AND state='waiting_approval'
              AND json_extract(payload, '$.run_id')=?
              AND json_extract(payload, '$.capability')=?
              AND json_extract(payload, '$.input_sha256')=?
            ORDER BY created_at DESC LIMIT 1""",
            (run_id, capability, input_sha256),
        ).fetchone()
        return self._record(row) if row is not None else None

    def _assert_run(self, spec: CapabilityCallSpec) -> None:
        row = self._connection.execute(
            """SELECT scope_id, payload FROM lifecycle_entities
            WHERE entity_type='agent_run' AND entity_id=?""",
            (spec.run_id,),
        ).fetchone()
        if row is None:
            raise CapabilityCallConflictError("parent AgentRun does not exist")
        payload = dict(json.loads(row["payload"]))
        if (
            str(row["scope_id"]) != spec.scope_id
            or str(payload.get("actor_id") or "") != spec.actor_id
            or str(payload.get("workflow_id") or "") != spec.workflow_id
        ):
            raise CapabilityCallConflictError(
                "CapabilityCall does not inherit parent AgentRun identity"
            )

    def _find(self, *, call_id: str = "", execution_key: str = "") -> Any | None:
        if execution_key:
            return self._connection.execute(
                """SELECT * FROM lifecycle_entities
                WHERE entity_type='capability_call'
                  AND json_extract(payload, '$.execution_key')=?""",
                (execution_key,),
            ).fetchone()
        return self._connection.execute(
            """SELECT * FROM lifecycle_entities
            WHERE entity_type='capability_call' AND entity_id=?""",
            (call_id,),
        ).fetchone()

    @staticmethod
    def _identity_payload(spec: CapabilityCallSpec) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "execution_key": spec.execution_key,
            "capability": spec.capability,
            "actor_id": spec.actor_id,
            "scope_id": spec.scope_id,
            "trace_id": spec.trace_id,
            "run_id": spec.run_id,
            "workflow_id": spec.workflow_id,
            "tool_call_id": spec.tool_call_id,
            "agent_ref": spec.agent_ref,
            "skill_ref": spec.skill_ref,
            "policy_ref": spec.policy_ref,
            "input_sha256": spec.input_sha256,
        }

    @classmethod
    def _assert_identity(cls, row: Any, expected: dict[str, Any]) -> None:
        actual = dict(json.loads(row["payload"]))
        if str(row["scope_id"]) != expected["scope_id"] or any(
            actual.get(field) != expected.get(field) for field in expected
        ):
            raise CapabilityCallConflictError("CapabilityCall replay identity does not match")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

    @staticmethod
    def _record(row: Any) -> CapabilityCallRecord:
        payload = dict(json.loads(row["payload"]))
        return CapabilityCallRecord(
            call_id=str(row["entity_id"]),
            execution_key=str(payload.get("execution_key") or ""),
            capability=str(payload.get("capability") or ""),
            actor_id=str(payload.get("actor_id") or ""),
            scope_id=str(row["scope_id"]),
            trace_id=str(payload.get("trace_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            workflow_id=str(payload.get("workflow_id") or ""),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            state=CapabilityCallState(str(row["state"])),
            version=int(row["version"]),
            agent_ref=str(payload.get("agent_ref") or ""),
            skill_ref=str(payload.get("skill_ref") or ""),
            policy_ref=str(payload.get("policy_ref") or ""),
            input_sha256=str(payload.get("input_sha256") or ""),
            result_payload=payload.get("result_payload"),
            tool_content=str(payload.get("tool_content") or ""),
            duration_ms=int(payload.get("duration_ms") or 0),
            error_code=str(payload.get("error_code") or ""),
        )


class CapabilityCallService:
    def __init__(
        self, store: SQLiteControlPlane, lifecycle: LifecycleService | None = None
    ) -> None:
        if lifecycle is None:
            from .lifecycle import LifecycleService

            lifecycle = LifecycleService(store)
        self.store = store
        self.lifecycle = lifecycle

    def create_or_get(self, spec: CapabilityCallSpec) -> tuple[CapabilityCallRecord, bool]:
        return self.store.capability_calls.create_or_get(spec)

    def get(self, call_id: str) -> CapabilityCallRecord:
        return self.store.capability_calls.get(call_id)

    def find_waiting(
        self,
        *,
        run_id: str,
        capability: str,
        input_sha256: str,
    ) -> CapabilityCallRecord | None:
        return self.store.capability_calls.find_waiting(
            run_id=run_id,
            capability=capability,
            input_sha256=input_sha256,
        )

    def transition(
        self,
        call_id: str,
        state: CapabilityCallState,
        *,
        command_id: str,
        expected_version: int,
        actor_id: str,
        trace_id: str,
        reason: str = "",
        payload_patch: dict[str, Any] | None = None,
    ) -> CapabilityCallRecord:
        self.lifecycle.transition(
            EntityType.CAPABILITY_CALL,
            call_id,
            state.value,
            command_id=command_id,
            expected_version=expected_version,
            actor_id=actor_id,
            trace_id=trace_id,
            reason=reason,
            payload_patch=payload_patch,
        )
        return self.get(call_id)
