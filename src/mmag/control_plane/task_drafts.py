"""Durable task drafts derived from structured meeting outputs."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3
    import threading


class TaskDraftState(StrEnum):
    DRAFT = "draft"
    COMMITTING = "committing"
    COMMITTED = "committed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class TaskDraft:
    id: str
    installation_id: str
    tenant_id: str
    scope_id: str
    channel_id: str
    root_id: str
    run_id: str
    requested_by: str
    title: str
    items: tuple[dict[str, Any], ...]
    content_hash: str
    state: TaskDraftState
    decided_by: str = ""
    task_ids: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0


class TaskDraftStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create(
        self,
        *,
        installation_id: str,
        tenant_id: str,
        scope_id: str,
        channel_id: str,
        root_id: str,
        run_id: str,
        requested_by: str,
        title: str,
        items: tuple[dict[str, Any], ...],
        content_hash: str,
    ) -> TaskDraft:
        if not all(
            (installation_id, tenant_id, scope_id, channel_id, root_id, run_id, requested_by)
        ) or not items:
            raise ValueError("task draft is incomplete")
        now = time.time()
        draft_id = uuid.uuid4().hex
        encoded_items = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self.connection.execute(
                """INSERT OR IGNORE INTO task_drafts
                (id, installation_id, tenant_id, scope_id, channel_id, root_id, run_id,
                 requested_by, title, items, content_hash, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                (
                    draft_id,
                    installation_id,
                    tenant_id,
                    scope_id,
                    channel_id,
                    root_id,
                    run_id,
                    requested_by,
                    title[:500],
                    encoded_items,
                    content_hash,
                    now,
                    now,
                ),
            )
            self.connection.commit()
        return self.get_by_run(
            run_id,
            installation_id=installation_id,
            tenant_id=tenant_id,
        )

    def get(self, draft_id: str) -> TaskDraft:
        row = self.connection.execute(
            "SELECT * FROM task_drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return self._record(row)

    def get_by_run(
        self, run_id: str, *, installation_id: str, tenant_id: str
    ) -> TaskDraft:
        row = self.connection.execute(
            """SELECT * FROM task_drafts
            WHERE installation_id=? AND tenant_id=? AND run_id=?""",
            (installation_id, tenant_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._record(row)

    def claim_commit(self, draft_id: str, *, actor_id: str) -> TaskDraft:
        current = self.get(draft_id)
        if current.requested_by != actor_id:
            raise PermissionError("只有草案创建者可以确认任务")
        if current.state is TaskDraftState.COMMITTED:
            return current
        if current.state is TaskDraftState.REJECTED:
            raise ValueError("任务草案已放弃")
        if current.state is TaskDraftState.COMMITTING and current.decided_by != actor_id:
            raise PermissionError("任务草案正在由其他用户处理")
        if current.state is TaskDraftState.DRAFT:
            with self.lock:
                cursor = self.connection.execute(
                    """UPDATE task_drafts SET state='committing', decided_by=?, updated_at=?
                    WHERE id=? AND state='draft'""",
                    (actor_id, time.time(), draft_id),
                )
                self.connection.commit()
            if cursor.rowcount != 1:
                return self.claim_commit(draft_id, actor_id=actor_id)
        return self.get(draft_id)

    def complete(self, draft_id: str, *, actor_id: str, task_ids: tuple[str, ...]) -> TaskDraft:
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE task_drafts SET state='committed', task_ids=?, updated_at=?
                WHERE id=? AND state='committing' AND decided_by=?""",
                (json.dumps(task_ids), time.time(), draft_id, actor_id),
            )
            self.connection.commit()
        if cursor.rowcount != 1:
            current = self.get(draft_id)
            if current.state is not TaskDraftState.COMMITTED:
                raise ValueError("任务草案无法完成提交")
        return self.get(draft_id)

    def reject(self, draft_id: str, *, actor_id: str) -> TaskDraft:
        current = self.get(draft_id)
        if current.requested_by != actor_id:
            raise PermissionError("只有草案创建者可以放弃任务")
        if current.state is TaskDraftState.REJECTED:
            return current
        if current.state is not TaskDraftState.DRAFT:
            raise ValueError("任务草案已开始提交或已经创建任务")
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE task_drafts SET state='rejected', decided_by=?, updated_at=?
                WHERE id=? AND state='draft'""",
                (actor_id, time.time(), draft_id),
            )
            self.connection.commit()
        if cursor.rowcount != 1:
            raise ValueError("任务草案已被处理")
        return self.get(draft_id)

    @staticmethod
    def _record(row) -> TaskDraft:
        raw_items = json.loads(row["items"] or "[]")
        raw_task_ids = json.loads(row["task_ids"] or "[]")
        normalized = json.dumps(
            raw_items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(normalized.encode()).hexdigest() != str(row["content_hash"]):
            raise ValueError("task draft content hash mismatch")
        return TaskDraft(
            id=str(row["id"]),
            installation_id=str(row["installation_id"]),
            tenant_id=str(row["tenant_id"]),
            scope_id=str(row["scope_id"]),
            channel_id=str(row["channel_id"]),
            root_id=str(row["root_id"]),
            run_id=str(row["run_id"]),
            requested_by=str(row["requested_by"]),
            title=str(row["title"]),
            items=tuple(dict(item) for item in raw_items if isinstance(item, dict)),
            content_hash=str(row["content_hash"]),
            state=TaskDraftState(str(row["state"])),
            decided_by=str(row["decided_by"]),
            task_ids=tuple(str(item) for item in raw_task_ids),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
