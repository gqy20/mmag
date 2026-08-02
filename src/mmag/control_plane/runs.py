"""Atomic persistence for governed AgentRun payloads."""

from __future__ import annotations

import json
import time
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class AgentRunStore:
    def __init__(self, connection: Any, lock: Any) -> None:
        self._connection = connection
        self._lock = lock

    def bind_snapshot(
        self,
        entity_id: str,
        *,
        snapshot: dict[str, Any],
        actor_id: str,
        trace_id: str,
        intent: str,
        capabilities: tuple[str, ...],
    ) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
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
