"""Personal WorkCase evidence and short-lived interaction sessions."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from .context import MattermostScopeResolver
from .models import (
    InteractionSession,
    InteractionStatus,
    ScopeKind,
    WorkCase,
    WorkCaseStatus,
)

if TYPE_CHECKING:
    import sqlite3
    import threading
    from collections.abc import Iterable, Mapping

_MAX_SAVED_PER_OWNER = 200
_MAX_CANDIDATES_PER_OWNER = 50
_CANDIDATE_RETENTION_SECONDS = 7 * 24 * 60 * 60


class WorkCaseStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create(
        self, *, installation_id: str, tenant_id: str, owner_id: str,
        scope_id: str, goal: str, result_summary: str, agent_name: str = "",
        skill_ref: str = "", personal_skill_ref: str = "",
        artifact_refs: Iterable[str] = (),
        source_run_id: str = "", source_message_id: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> WorkCase:
        if not all((installation_id, tenant_id, owner_id, scope_id, goal.strip())):
            raise ValueError("WorkCase identity, scope and goal are required")
        parsed_installation, parsed_tenant, parsed_kind, resource_id = (
            MattermostScopeResolver.parse(scope_id)
        )
        if (parsed_kind is not ScopeKind.PERSONAL or parsed_installation != installation_id
                or parsed_tenant != tenant_id or resource_id != owner_id):
            raise PermissionError("WorkCase identity does not match its Personal Scope")
        clean_goal = goal.strip()[:4_000]
        clean_result = result_summary.strip()[:12_000]
        goal_hash = hashlib.sha256(clean_goal.encode()).hexdigest()
        result_hash = hashlib.sha256(clean_result.encode()).hexdigest()
        identifier, now = uuid.uuid4().hex, time.time()
        with self.lock:
            self._cleanup_candidates_locked(owner_id, now=now)
            if source_run_id:
                existing = self.connection.execute(
                    "SELECT id FROM work_cases WHERE owner_id=? AND source_run_id=? LIMIT 1",
                    (owner_id, source_run_id),
                ).fetchone()
                if existing is not None:
                    return self.get(str(existing["id"]), owner_id=owner_id)
            self.connection.execute(
                """INSERT INTO work_cases
                (id, installation_id, tenant_id, owner_id, scope_id, goal,
                 result_summary, agent_name, skill_ref, personal_skill_ref,
                 artifact_refs, source_run_id, source_message_id, goal_hash,
                 result_hash, provenance, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'candidate', ?, ?)""",
                (identifier, installation_id, tenant_id, owner_id, scope_id,
                 clean_goal, clean_result, agent_name,
                 skill_ref, personal_skill_ref,
                 json.dumps(tuple(dict.fromkeys(artifact_refs)), ensure_ascii=False),
                 source_run_id[:256], source_message_id[:64], goal_hash, result_hash,
                 json.dumps(dict(provenance or {}), ensure_ascii=False, default=str), now, now),
            )
            self.connection.commit()
        return self.get(identifier, owner_id=owner_id)

    def get(self, case_id: str, *, owner_id: str) -> WorkCase:
        row = self.connection.execute(
            "SELECT * FROM work_cases WHERE id=? AND owner_id=?", (case_id, owner_id)
        ).fetchone()
        if row is None:
            raise KeyError(case_id)
        return self._record(row)

    def update(
        self, case_id: str, *, owner_id: str,
        status: WorkCaseStatus | None = None, feedback: str | None = None,
    ) -> WorkCase:
        current = self.get(case_id, owner_id=owner_id)
        next_status = status or current.status
        next_feedback = current.feedback if feedback is None else feedback.strip()[:40]
        with self.lock:
            if next_status is WorkCaseStatus.SAVED and current.status is not WorkCaseStatus.SAVED:
                count = self.connection.execute(
                    "SELECT COUNT(*) FROM work_cases WHERE owner_id=? AND status='saved'",
                    (owner_id,),
                ).fetchone()[0]
                if int(count) >= _MAX_SAVED_PER_OWNER:
                    raise ValueError("personal WorkCase saved quota is exhausted")
            self.connection.execute(
                "UPDATE work_cases SET status=?, feedback=?, updated_at=? WHERE id=? AND owner_id=?",
                (next_status.value, next_feedback, time.time(), case_id, owner_id),
            )
            self.connection.commit()
        return self.get(case_id, owner_id=owner_id)

    def list_saved(
        self, *, installation_id: str, tenant_id: str, owner_id: str, limit: int = 20,
    ) -> tuple[WorkCase, ...]:
        rows = self.connection.execute(
            """SELECT * FROM work_cases WHERE installation_id=? AND tenant_id=?
            AND owner_id=? AND status='saved' ORDER BY updated_at DESC LIMIT ?""",
            (installation_id, tenant_id, owner_id, max(1, min(limit, 100))),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def cleanup_candidates(self, *, owner_id: str) -> int:
        with self.lock:
            deleted = self._cleanup_candidates_locked(owner_id, now=time.time())
            self.connection.commit()
        return deleted

    def _cleanup_candidates_locked(self, owner_id: str, *, now: float) -> int:
        expired = self.connection.execute(
            """DELETE FROM work_cases WHERE owner_id=? AND status='candidate'
            AND updated_at<?""",
            (owner_id, now - _CANDIDATE_RETENTION_SECONDS),
        ).rowcount
        overflow = self.connection.execute(
            """DELETE FROM work_cases WHERE id IN (
                SELECT id FROM work_cases WHERE owner_id=? AND status='candidate'
                ORDER BY updated_at DESC LIMIT -1 OFFSET ?
            )""",
            (owner_id, _MAX_CANDIDATES_PER_OWNER),
        ).rowcount
        return int(expired + overflow)

    def similar_saved(self, case: WorkCase, *, limit: int = 5) -> tuple[WorkCase, ...]:
        rows = self.connection.execute(
            """SELECT * FROM work_cases WHERE installation_id=? AND tenant_id=?
            AND owner_id=? AND status='saved' AND agent_name=? AND skill_ref=?
            ORDER BY updated_at DESC LIMIT ?""",
            (case.installation_id, case.tenant_id, case.owner_id, case.agent_name,
             case.skill_ref, max(1, min(limit, 20))),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _record(row) -> WorkCase:
        return WorkCase(
            id=row["id"], installation_id=row["installation_id"], tenant_id=row["tenant_id"],
            owner_id=row["owner_id"], scope_id=row["scope_id"], goal=row["goal"],
            result_summary=row["result_summary"], agent_name=row["agent_name"],
            skill_ref=row["skill_ref"], personal_skill_ref=row["personal_skill_ref"],
            source_run_id=row["source_run_id"], source_message_id=row["source_message_id"],
            goal_hash=row["goal_hash"], result_hash=row["result_hash"],
            provenance=json.loads(row["provenance"]),
            artifact_refs=tuple(json.loads(row["artifact_refs"])), feedback=row["feedback"],
            status=WorkCaseStatus(row["status"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


class InteractionSessionStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def open(
        self, *, installation_id: str, tenant_id: str, owner_id: str, scope_id: str,
        conversation_id: str, kind: str, payload: Mapping[str, Any], ttl_seconds: int = 900,
    ) -> InteractionSession:
        parsed_installation, parsed_tenant, parsed_kind, resource_id = (
            MattermostScopeResolver.parse(scope_id)
        )
        if (parsed_kind is not ScopeKind.PERSONAL or parsed_installation != installation_id
                or parsed_tenant != tenant_id or resource_id != owner_id):
            raise PermissionError("interaction identity does not match its Personal Scope")
        now, identifier = time.time(), uuid.uuid4().hex
        with self.lock:
            self.connection.execute(
                """UPDATE interaction_sessions SET status='cancelled', updated_at=?
                WHERE installation_id=? AND tenant_id=? AND owner_id=?
                AND conversation_id=? AND status='open'""",
                (now, installation_id, tenant_id, owner_id, conversation_id),
            )
            self.connection.execute(
                """INSERT INTO interaction_sessions
                (id, installation_id, tenant_id, owner_id, scope_id, conversation_id,
                 kind, payload, expires_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (identifier, installation_id, tenant_id, owner_id, scope_id, conversation_id,
                 kind, json.dumps(dict(payload), ensure_ascii=False), now + ttl_seconds, now, now),
            )
            self.connection.commit()
        session = self.get_open(installation_id=installation_id, tenant_id=tenant_id,
                                owner_id=owner_id, conversation_id=conversation_id)
        if session is None:
            raise RuntimeError("interaction session was not persisted")
        return session

    def get_open(
        self, *, installation_id: str, tenant_id: str, owner_id: str, conversation_id: str,
    ) -> InteractionSession | None:
        row = self.connection.execute(
            """SELECT * FROM interaction_sessions WHERE installation_id=? AND tenant_id=?
            AND owner_id=? AND conversation_id=? AND status='open'
            ORDER BY updated_at DESC LIMIT 1""",
            (installation_id, tenant_id, owner_id, conversation_id),
        ).fetchone()
        if row is None:
            return None
        session = self._record(row)
        if session.expires_at < time.time():
            self.complete(session.id, status=InteractionStatus.CANCELLED)
            return None
        return session

    def complete(
        self, session_id: str, *, status: InteractionStatus = InteractionStatus.COMPLETED,
    ) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE interaction_sessions SET status=?, updated_at=? WHERE id=?",
                (status.value, time.time(), session_id),
            )
            self.connection.commit()

    def cancel_open(
        self, *, installation_id: str, tenant_id: str, owner_id: str, conversation_id: str,
    ) -> int:
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE interaction_sessions SET status='cancelled', updated_at=?
                WHERE installation_id=? AND tenant_id=? AND owner_id=?
                AND conversation_id=? AND status='open'""",
                (time.time(), installation_id, tenant_id, owner_id, conversation_id),
            )
            self.connection.commit()
        return cursor.rowcount

    @staticmethod
    def _record(row) -> InteractionSession:
        return InteractionSession(
            id=row["id"], installation_id=row["installation_id"], tenant_id=row["tenant_id"],
            owner_id=row["owner_id"], scope_id=row["scope_id"],
            conversation_id=row["conversation_id"], kind=row["kind"],
            payload=json.loads(row["payload"]), expires_at=float(row["expires_at"]),
            status=InteractionStatus(row["status"]),
        )
