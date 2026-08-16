"""Atomic persistence for governed AgentRun payloads."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Protocol

from .models import AgentRunRecord, AgentRunSpec, AgentRunState, EntityType

if TYPE_CHECKING:
    from .lifecycle import LifecycleService
    from .store import SQLiteControlPlane


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def delegation_execution_key(spec: AgentRunSpec) -> str:
    """Derive the stable child-run key exclusively from governed identity fields."""
    if not spec.parent_run_id or not spec.parent_tool_call_id:
        return ""
    identity = {
        "parent_run_id": spec.parent_run_id,
        "parent_tool_call_id": spec.parent_tool_call_id,
        "agent_ref": spec.agent_ref,
        "skill_ref": spec.skill_ref,
        "package_snapshot": spec.package_snapshot,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class AgentRunConflictError(RuntimeError):
    """A run identifier or replay key was reused with different trusted identity."""


class AgentRunInProgressError(RuntimeError):
    """A replay reached a child AgentRun that is already executing."""


class AgentRunTerminalError(RuntimeError):
    """A failed or cancelled child AgentRun cannot be executed again."""


class LifecycleAuditWriter(Protocol):
    def __call__(
        self,
        *,
        entity_type: EntityType,
        entity_id: str,
        action: str,
        from_state: str,
        to_state: str,
        version: int,
        scope_id: str,
        actor_id: str,
        trace_id: str,
        payload: dict[str, Any],
        command_id: str = "",
        recovery: bool = False,
        created_at: float | None = None,
    ) -> str: ...


class AgentRunStore:
    def __init__(
        self,
        connection: Any,
        lock: Any,
        lifecycle_audit: LifecycleAuditWriter,
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._lifecycle_audit = lifecycle_audit

    def create_or_get(self, spec: AgentRunSpec) -> tuple[AgentRunRecord, bool]:
        """Atomically create a run or return its replay-stable existing record."""
        execution_key = delegation_execution_key(spec)
        payload = self._identity_payload(spec, execution_key)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._find_row(execution_key=execution_key) if execution_key else None
                if existing is None:
                    existing = self._find_row(run_id=spec.run_id)
                if existing is not None:
                    self._assert_same_identity(existing, payload, requested_run_id=spec.run_id)
                    self._connection.commit()
                    return self._record(existing), False
                self._assert_parent(spec)
                now = time.time()
                self._connection.execute(
                    """INSERT INTO lifecycle_entities
                    (entity_type, entity_id, scope_id, state, payload, created_at, updated_at)
                    VALUES ('agent_run', ?, ?, 'queued', ?, ?, ?)""",
                    (spec.run_id, spec.scope_id, _json(payload), now, now),
                )
                self._lifecycle_audit(
                    entity_type=EntityType.AGENT_RUN,
                    entity_id=spec.run_id,
                    action="created",
                    from_state="",
                    to_state=AgentRunState.QUEUED.value,
                    version=0,
                    scope_id=spec.scope_id,
                    actor_id=spec.actor_id,
                    trace_id=spec.trace_id,
                    payload=payload,
                    created_at=now,
                )
                row = self._find_row(run_id=spec.run_id)
                if row is None:  # pragma: no cover - guarded by the insert above
                    raise RuntimeError("created AgentRun could not be read")
                self._connection.commit()
                return self._record(row), True
            except Exception:
                self._connection.rollback()
                raise

    def get(self, run_id: str) -> AgentRunRecord:
        row = self._find_row(run_id=run_id)
        if row is None:
            raise KeyError(f"agent_run:{run_id}")
        return self._record(row)

    def find_by_execution_key(self, execution_key: str) -> AgentRunRecord | None:
        if not execution_key:
            return None
        row = self._find_row(execution_key=execution_key)
        return self._record(row) if row is not None else None

    def bind_snapshot(
        self,
        entity_id: str,
        *,
        snapshot: dict[str, Any],
        actor_id: str,
        scope_id: str = "",
        conversation_id: str = "",
        trace_id: str,
        intent: str,
        capabilities: tuple[str, ...],
        workflow_id: str = "",
    ) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._find_row(run_id=entity_id)
                if row is None:
                    raise KeyError(f"agent_run:{entity_id}")
                current_scope = str(row["scope_id"] or "")
                if (
                    scope_id
                    and current_scope
                    and current_scope != scope_id
                    and current_scope != conversation_id
                ):
                    raise PermissionError("agent run scope is immutable")
                if scope_id and current_scope != scope_id:
                    self._connection.execute(
                        "UPDATE lifecycle_entities SET scope_id=? WHERE entity_type='agent_run' AND entity_id=?",
                        (scope_id, entity_id),
                    )
                payload = self._payload(entity_id)
                required = payload.get("required_snapshot")
                existing = payload.get("snapshot")
                if isinstance(required, dict) and required != snapshot:
                    raise RuntimeError("agent run package snapshot does not match replay source")
                if isinstance(existing, dict) and existing != snapshot:
                    raise RuntimeError("agent run package snapshot is immutable")
                payload.update(
                    {
                        "snapshot": snapshot,
                        "actor_id": actor_id,
                        "trace_id": trace_id,
                        "workflow_id": workflow_id or entity_id,
                        "intent": intent,
                        "capabilities": list(capabilities),
                    }
                )
                self._update(entity_id, payload)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def record_result(
        self,
        entity_id: str,
        *,
        status: str,
        usage: dict[str, Any],
        capability_calls: int,
        artifact_count: int,
    ) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                payload = self._payload(entity_id)
                payload.update(
                    {
                        "runtime_status": status,
                        "usage": usage,
                        "capability_calls": capability_calls,
                        "artifact_count": artifact_count,
                    }
                )
                self._update(entity_id, payload)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def record_failure(self, entity_id: str, *, error_code: str) -> None:
        with self._lock:
            payload = self._payload(entity_id)
            payload.update({"runtime_status": "failed", "error_code": error_code})
            self._update(entity_id, payload)
            self._connection.commit()

    def _payload(self, entity_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload FROM lifecycle_entities "
            "WHERE entity_type='agent_run' AND entity_id=?",
            (entity_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"agent_run:{entity_id}")
        return dict(json.loads(row["payload"]))

    def _update(self, entity_id: str, payload: dict[str, Any]) -> None:
        self._connection.execute(
            "UPDATE lifecycle_entities SET payload=?, updated_at=? "
            "WHERE entity_type='agent_run' AND entity_id=?",
            (_json(payload), time.time(), entity_id),
        )

    def _assert_parent(self, spec: AgentRunSpec) -> None:
        if not spec.parent_run_id:
            return
        parent = self._find_row(run_id=spec.parent_run_id)
        if parent is None:
            raise AgentRunConflictError("parent AgentRun does not exist")
        payload = dict(json.loads(parent["payload"]))
        if (
            str(parent["scope_id"]) != spec.scope_id
            or str(payload.get("workflow_id") or "") != spec.workflow_id
            or str(payload.get("actor_id") or "") != spec.actor_id
        ):
            raise AgentRunConflictError("child AgentRun does not inherit parent identity")

    def _find_row(self, *, run_id: str = "", execution_key: str = "") -> Any | None:
        if execution_key:
            return self._connection.execute(
                """SELECT * FROM lifecycle_entities
                WHERE entity_type='agent_run'
                  AND json_extract(payload, '$.execution_key')=?""",
                (execution_key,),
            ).fetchone()
        return self._connection.execute(
            """SELECT * FROM lifecycle_entities
            WHERE entity_type='agent_run' AND entity_id=?""",
            (run_id,),
        ).fetchone()

    @staticmethod
    def _identity_payload(spec: AgentRunSpec, execution_key: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "workflow_id": spec.workflow_id,
            "scope_id": spec.scope_id,
            "parent_run_id": spec.parent_run_id,
            "parent_tool_call_id": spec.parent_tool_call_id,
            "execution_key": execution_key,
            "thread_id": spec.thread_id,
            "actor_id": spec.actor_id,
            "trace_id": spec.trace_id,
            "agent_ref": spec.agent_ref,
            "skill_ref": spec.skill_ref,
            "snapshot": dict(spec.package_snapshot),
        }

    @staticmethod
    def _assert_same_identity(
        row: Any,
        expected: dict[str, Any],
        *,
        requested_run_id: str,
    ) -> None:
        actual = dict(json.loads(row["payload"]))
        immutable = (
            "workflow_id",
            "scope_id",
            "parent_run_id",
            "parent_tool_call_id",
            "execution_key",
            "actor_id",
            "agent_ref",
            "skill_ref",
            "snapshot",
        )
        if str(row["scope_id"]) != expected["scope_id"]:
            raise AgentRunConflictError("AgentRun scope is immutable")
        if any(actual.get(field) != expected.get(field) for field in immutable):
            raise AgentRunConflictError("AgentRun replay identity does not match")
        if str(row["entity_id"]) == requested_run_id and any(
            actual.get(field) != expected.get(field) for field in ("trace_id", "thread_id")
        ):
            raise AgentRunConflictError("AgentRun thread identity is immutable")

    @staticmethod
    def _record(row: Any) -> AgentRunRecord:
        payload = dict(json.loads(row["payload"]))
        return AgentRunRecord(
            run_id=str(row["entity_id"]),
            workflow_id=str(payload.get("workflow_id") or ""),
            actor_id=str(payload.get("actor_id") or ""),
            scope_id=str(row["scope_id"]),
            trace_id=str(payload.get("trace_id") or ""),
            thread_id=str(payload.get("thread_id") or ""),
            agent_ref=str(payload.get("agent_ref") or ""),
            package_snapshot=dict(payload.get("snapshot") or {}),
            state=AgentRunState(str(row["state"])),
            version=int(row["version"]),
            execution_key=str(payload.get("execution_key") or ""),
            parent_run_id=str(payload.get("parent_run_id") or ""),
            parent_tool_call_id=str(payload.get("parent_tool_call_id") or ""),
            skill_ref=str(payload.get("skill_ref") or ""),
            result_envelope=dict(payload.get("dispatch_result") or {}),
        )


class AgentRunService:
    """Typed creation and transition boundary for durable AgentRuns."""

    def __init__(
        self,
        store: SQLiteControlPlane,
        lifecycle: LifecycleService | None = None,
    ) -> None:
        if lifecycle is None:
            from .lifecycle import LifecycleService

            lifecycle = LifecycleService(store)
        self.store = store
        self.lifecycle = lifecycle

    def create_or_get(self, spec: AgentRunSpec) -> tuple[AgentRunRecord, bool]:
        return self.store.runs.create_or_get(spec)

    def get(self, run_id: str) -> AgentRunRecord:
        return self.store.runs.get(run_id)

    def transition(
        self,
        run_id: str,
        state: AgentRunState,
        *,
        command_id: str,
        expected_version: int | None = None,
        reason: str = "",
        actor_id: str = "",
        trace_id: str = "",
        payload_patch: dict[str, Any] | None = None,
    ) -> AgentRunRecord:
        self.lifecycle.transition(
            EntityType.AGENT_RUN,
            run_id,
            state.value,
            command_id=command_id,
            expected_version=expected_version,
            reason=reason,
            actor_id=actor_id,
            trace_id=trace_id,
            payload_patch=payload_patch,
        )
        return self.store.runs.get(run_id)

    def finish(
        self,
        run_id: str,
        state: AgentRunState,
        *,
        result_envelope: dict[str, Any],
        command_id: str,
        expected_version: int,
        actor_id: str,
        trace_id: str,
    ) -> AgentRunRecord:
        if state not in {AgentRunState.SUCCEEDED, AgentRunState.EXHAUSTED}:
            raise ValueError("AgentRun finish requires a successful terminal state")
        return self.transition(
            run_id,
            state,
            command_id=command_id,
            expected_version=expected_version,
            actor_id=actor_id,
            trace_id=trace_id,
            payload_patch={
                "runtime_status": state.value,
                "dispatch_result": result_envelope,
            },
        )

    def fail(
        self,
        run_id: str,
        *,
        error_code: str,
        command_id: str,
        expected_version: int,
        actor_id: str,
        trace_id: str,
    ) -> AgentRunRecord:
        return self.transition(
            run_id,
            AgentRunState.FAILED,
            command_id=command_id,
            expected_version=expected_version,
            actor_id=actor_id,
            trace_id=trace_id,
            payload_patch={"runtime_status": "failed", "error_code": error_code},
        )
