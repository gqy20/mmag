"""Durable owner decisions for high-risk Digital Persona replies."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from .models import PersonaReplyRequest, PersonaReplyState

if TYPE_CHECKING:
    import sqlite3
    import threading


class PersonaReplyStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create(
        self,
        *,
        installation_id: str,
        tenant_id: str,
        persona_ref: str,
        persona_hash: str,
        owner_id: str,
        requester_id: str,
        requester_username: str,
        source_scope_id: str,
        source_channel_id: str,
        source_root_id: str,
        source_status_post_id: str,
        question: str,
        draft_text: str,
        approval_reason: str,
        ttl_seconds: int = 600,
    ) -> PersonaReplyRequest:
        if not all(
            (
                installation_id,
                tenant_id,
                persona_ref,
                persona_hash,
                owner_id,
                requester_id,
                source_scope_id,
                source_channel_id,
                source_root_id,
                question.strip(),
                draft_text.strip(),
            )
        ):
            raise ValueError("persona reply request is incomplete")
        if ttl_seconds < 30 or ttl_seconds > 3600:
            raise ValueError("persona reply request TTL must be between 30 and 3600 seconds")
        now = time.time()
        request_id = uuid.uuid4().hex
        with self.lock:
            self.connection.execute(
                """INSERT INTO persona_reply_requests
                (id, installation_id, tenant_id, persona_ref, persona_hash, owner_id,
                 requester_id, requester_username, source_scope_id, source_channel_id,
                 source_root_id, source_status_post_id, question, draft_text,
                 approval_reason, expires_at, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    request_id,
                    installation_id,
                    tenant_id,
                    persona_ref,
                    persona_hash,
                    owner_id,
                    requester_id,
                    requester_username[:64],
                    source_scope_id,
                    source_channel_id,
                    source_root_id,
                    source_status_post_id,
                    question.strip()[:8_000],
                    draft_text.strip()[:16_000],
                    approval_reason.strip()[:500],
                    now + ttl_seconds,
                    now,
                    now,
                ),
            )
            self.connection.commit()
        return self.get(request_id)

    def get(self, request_id: str) -> PersonaReplyRequest:
        row = self.connection.execute(
            "SELECT * FROM persona_reply_requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return self._record(row)

    def list_states(self, *states: PersonaReplyState) -> tuple[PersonaReplyRequest, ...]:
        if not states:
            return ()
        placeholders = ",".join("?" for _ in states)
        rows = self.connection.execute(
            f"SELECT * FROM persona_reply_requests WHERE state IN ({placeholders}) "
            "ORDER BY created_at",
            tuple(state.value for state in states),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def expire_pending(self, *, now: float | None = None) -> tuple[PersonaReplyRequest, ...]:
        timestamp = now if now is not None else time.time()
        with self.lock:
            rows = self.connection.execute(
                """SELECT id FROM persona_reply_requests
                WHERE state='pending' AND expires_at<? ORDER BY created_at""",
                (timestamp,),
            ).fetchall()
            ids = tuple(str(row["id"]) for row in rows)
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.connection.execute(
                    f"""UPDATE persona_reply_requests SET state='expired',
                    last_error='approval expired', updated_at=?
                    WHERE id IN ({placeholders}) AND state='pending'""",
                    (timestamp, *ids),
                )
                self.connection.commit()
        return tuple(self.get(request_id) for request_id in ids)

    def decide(
        self,
        request_id: str,
        *,
        actor_id: str,
        approved: bool,
        draft_text: str = "",
        now: float | None = None,
    ) -> PersonaReplyRequest:
        current = self.get(request_id)
        timestamp = now if now is not None else time.time()
        if current.owner_id != actor_id:
            raise PermissionError("only the persona owner can decide this reply")
        if current.state is not PersonaReplyState.PENDING:
            raise ValueError("persona reply request was already decided")
        if current.expires_at < timestamp:
            self._finish(request_id, PersonaReplyState.EXPIRED, "", "approval expired")
            raise ValueError("persona reply request has expired")
        final_draft = draft_text.strip()[:16_000] if approved and draft_text.strip() else ""
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE persona_reply_requests
                SET state=?, draft_text=CASE WHEN ?='' THEN draft_text ELSE ? END,
                    decision_by=?, decided_at=?, updated_at=?
                WHERE id=? AND state='pending'""",
                (
                    PersonaReplyState.APPROVED.value
                    if approved
                    else PersonaReplyState.REJECTED.value,
                    final_draft,
                    final_draft,
                    actor_id,
                    timestamp,
                    timestamp,
                    request_id,
                ),
            )
            self.connection.commit()
        if cursor.rowcount != 1:
            raise ValueError("persona reply request was already decided")
        return self.get(request_id)

    def mark_delivered(self, request_id: str) -> PersonaReplyRequest:
        with self.lock:
            self.connection.execute(
                """UPDATE persona_reply_requests SET state='delivered', last_error='',
                updated_at=? WHERE id=? AND state='approved'""",
                (time.time(), request_id),
            )
            self.connection.commit()
        return self.get(request_id)

    def mark_failed(self, request_id: str, error: str) -> PersonaReplyRequest:
        return self._finish(request_id, PersonaReplyState.FAILED, "", error[:500])

    def set_approval_target(
        self, request_id: str, *, channel_id: str = "", post_id: str = ""
    ) -> PersonaReplyRequest:
        if not channel_id and not post_id:
            raise ValueError("persona approval channel or post ID is required")
        with self.lock:
            self.connection.execute(
                """UPDATE persona_reply_requests SET
                owner_channel_id=CASE WHEN ?='' THEN owner_channel_id ELSE ? END,
                owner_approval_post_id=CASE WHEN ?='' THEN owner_approval_post_id ELSE ? END,
                updated_at=?
                WHERE id=? AND state='pending'""",
                (channel_id, channel_id, post_id, post_id, time.time(), request_id),
            )
            self.connection.commit()
        return self.get(request_id)

    def retry_failed(self, request_id: str, *, actor_id: str) -> PersonaReplyRequest:
        current = self.get(request_id)
        if current.owner_id != actor_id or not current.decision_by:
            raise PermissionError("only a decided reply can be retried by its owner")
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE persona_reply_requests SET state='approved', last_error='',
                updated_at=? WHERE id=? AND state='failed'""",
                (time.time(), request_id),
            )
            self.connection.commit()
        if cursor.rowcount != 1:
            raise ValueError("persona reply is not retryable")
        return self.get(request_id)

    def _finish(
        self,
        request_id: str,
        state: PersonaReplyState,
        actor_id: str,
        error: str,
    ) -> PersonaReplyRequest:
        with self.lock:
            self.connection.execute(
                """UPDATE persona_reply_requests SET state=?,
                decision_by=CASE WHEN ?='' THEN decision_by ELSE ? END,
                decided_at=CASE WHEN decided_at=0 THEN ? ELSE decided_at END,
                last_error=?, updated_at=? WHERE id=?""",
                (state.value, actor_id, actor_id, time.time(), error, time.time(), request_id),
            )
            self.connection.commit()
        return self.get(request_id)

    @staticmethod
    def _record(row) -> PersonaReplyRequest:
        return PersonaReplyRequest(
            id=row["id"],
            installation_id=row["installation_id"],
            tenant_id=row["tenant_id"],
            persona_ref=row["persona_ref"],
            persona_hash=row["persona_hash"],
            owner_id=row["owner_id"],
            requester_id=row["requester_id"],
            requester_username=row["requester_username"],
            source_scope_id=row["source_scope_id"],
            source_channel_id=row["source_channel_id"],
            source_root_id=row["source_root_id"],
            source_status_post_id=row["source_status_post_id"],
            owner_approval_post_id=row["owner_approval_post_id"],
            owner_channel_id=row["owner_channel_id"],
            question=row["question"],
            draft_text=row["draft_text"],
            approval_reason=row["approval_reason"],
            expires_at=float(row["expires_at"]),
            state=PersonaReplyState(row["state"]),
            decision_by=row["decision_by"],
            decided_at=float(row["decided_at"]),
            last_error=row["last_error"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
