"""Personal WorkCase evidence and short-lived interaction sessions."""

from __future__ import annotations

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


class WorkCaseStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create(
        self, *, installation_id: str, tenant_id: str, owner_id: str,
        scope_id: str, goal: str, result_summary: str, agent_name: str = "",
        skill_ref: str = "", personal_skill_ref: str = "",
        artifact_refs: Iterable[str] = (),
    ) -> WorkCase:
        if not all((installation_id, tenant_id, owner_id, scope_id, goal.strip())):
            raise ValueError("WorkCase identity, scope and goal are required")
        parsed_installation, parsed_tenant, parsed_kind, resource_id = (
            MattermostScopeResolver.parse(scope_id)
        )
        if (parsed_kind is not ScopeKind.PERSONAL or parsed_installation != installation_id
                or parsed_tenant != tenant_id or resource_id != owner_id):
            raise PermissionError("WorkCase identity does not match its Personal Scope")
        identifier, now = uuid.uuid4().hex, time.time()
        with self.lock:
            self.connection.execute(
                """INSERT INTO work_cases
                (id, installation_id, tenant_id, owner_id, scope_id, goal,
                 result_summary, agent_name, skill_ref, personal_skill_ref,
                 artifact_refs, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)""",
                (identifier, installation_id, tenant_id, owner_id, scope_id,
                 goal.strip()[:4_000], result_summary.strip()[:12_000], agent_name,
                 skill_ref, personal_skill_ref,
                 json.dumps(tuple(dict.fromkeys(artifact_refs)), ensure_ascii=False), now, now),
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
            artifact_refs=tuple(json.loads(row["artifact_refs"])), feedback=row["feedback"],
            status=WorkCaseStatus(row["status"]), created_at=float(row["created_at"]),
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

    @staticmethod
    def _record(row) -> InteractionSession:
        return InteractionSession(
            id=row["id"], installation_id=row["installation_id"], tenant_id=row["tenant_id"],
            owner_id=row["owner_id"], scope_id=row["scope_id"],
            conversation_id=row["conversation_id"], kind=row["kind"],
            payload=json.loads(row["payload"]), expires_at=float(row["expires_at"]),
            status=InteractionStatus(row["status"]),
        )
