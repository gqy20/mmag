"""Focused repositories extracted from the original Memory facade."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

_PREFERENCE_REF = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(?:@[0-9]+\.[0-9]+\.[0-9]+)?$")
_LANGUAGES = frozenset({"auto", "zh-CN", "en-US"})
_RESPONSE_STYLES = frozenset({"concise", "detailed", "formal", "casual"})


def normalize_personal_preferences(value: object) -> dict[str, object]:
    """Return the bounded preference contract accepted by routing and prompts."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, object] = {}
    language = str(value.get("language") or "")
    if language in _LANGUAGES:
        normalized["language"] = language
    response_style = str(value.get("response_style") or "")
    if response_style in _RESPONSE_STYLES:
        normalized["response_style"] = response_style
    for key in ("preferred_agents", "preferred_skills"):
        raw = value.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        refs = tuple(
            dict.fromkeys(
                str(item).lower()
                for item in raw[:10]
                if isinstance(item, str) and _PREFERENCE_REF.fullmatch(item.lower())
            )
        )
        if refs:
            normalized[key] = refs
    return normalized


@dataclass(frozen=True, slots=True)
class MessageRepository:
    connection: sqlite3.Connection
    installation_id: str
    tenant_id: str

    def contains(self, message_id: str) -> bool:
        if not message_id:
            return False
        row = self.connection.execute(
            """SELECT 1 FROM message_log
            WHERE installation_id=? AND tenant_id=? AND id=?""",
            (self.installation_id, self.tenant_id, message_id),
        ).fetchone()
        return row is not None

    def recent(self, conversation_id: str, limit: int) -> list[dict]:
        rows = self.connection.execute(
            """SELECT * FROM message_log
            WHERE installation_id=? AND tenant_id=? AND channel_id=?
            ORDER BY create_at DESC LIMIT ?""",
            (self.installation_id, self.tenant_id, conversation_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def actor_for(self, message_id: str) -> str | None:
        if not message_id:
            return None
        row = self.connection.execute(
            """SELECT user_id FROM message_log
            WHERE installation_id=? AND tenant_id=? AND id=?""",
            (self.installation_id, self.tenant_id, message_id),
        ).fetchone()
        return str(row["user_id"]) if row else None

    def latest_timestamp(self, conversation_id: str) -> float:
        row = self.connection.execute(
            """SELECT MAX(create_at) AS ts FROM message_log
            WHERE installation_id=? AND tenant_id=? AND channel_id=?""",
            (self.installation_id, self.tenant_id, conversation_id),
        ).fetchone()
        return float(row["ts"]) if row and row["ts"] else 0.0


@dataclass(frozen=True, slots=True)
class ProfileRepository:
    connection: sqlite3.Connection
    installation_id: str
    tenant_id: str

    def get(self, user_id: str, *, decode: bool = False) -> dict:
        row = self.connection.execute(
            """SELECT * FROM user_profiles
            WHERE installation_id=? AND tenant_id=? AND user_id=?""",
            (self.installation_id, self.tenant_id, user_id),
        ).fetchone()
        profile = dict(row) if row else {}
        if not profile or not decode:
            return profile
        for key, default in (("topics", []), ("active_hours", {})):
            try:
                profile[key] = json.loads(profile.get(key) or "")
            except (TypeError, json.JSONDecodeError):
                profile[key] = default
        try:
            profile["preferences"] = normalize_personal_preferences(
                json.loads(profile.get("preferences") or "{}")
            )
        except (TypeError, json.JSONDecodeError):
            profile["preferences"] = {}
        return profile

    def preferences(self, user_id: str) -> dict[str, object]:
        row = self.connection.execute(
            """SELECT preferences FROM user_profiles
            WHERE installation_id=? AND tenant_id=? AND user_id=?""",
            (self.installation_id, self.tenant_id, user_id),
        ).fetchone()
        if row is None:
            return {}
        try:
            return normalize_personal_preferences(json.loads(row["preferences"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}

    def set_preferences(self, user_id: str, value: object) -> dict[str, object]:
        preferences = normalize_personal_preferences(value)
        now = time.time()
        self.connection.execute(
            """INSERT INTO user_profiles
            (installation_id, tenant_id, user_id, preferences, first_seen, last_interaction)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(installation_id, tenant_id, user_id) DO UPDATE SET
              preferences=excluded.preferences,
              last_interaction=excluded.last_interaction""",
            (
                self.installation_id,
                self.tenant_id,
                user_id,
                json.dumps(preferences, ensure_ascii=False),
                now,
                now,
            ),
        )
        self.connection.commit()
        return preferences


@dataclass(frozen=True, slots=True)
class KnowledgeRepository:
    connection: sqlite3.Connection
    installation_id: str
    tenant_id: str

    def recent(self, scope_id: str, limit: int = 10) -> list[dict]:
        rows = self.connection.execute(
            """SELECT * FROM team_knowledge
            WHERE installation_id=? AND tenant_id=? AND channel_id=?
            ORDER BY updated_at DESC LIMIT ?""",
            (self.installation_id, self.tenant_id, scope_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


@dataclass(frozen=True, slots=True)
class SummaryRepository:
    connection: sqlite3.Connection
    installation_id: str
    tenant_id: str

    def latest(self, conversation_id: str) -> str:
        row = self.connection.execute(
            """SELECT summary FROM conversation_segments
            WHERE installation_id=? AND tenant_id=? AND channel_id=? AND status='active'
            ORDER BY started_at DESC LIMIT 1""",
            (self.installation_id, self.tenant_id, conversation_id),
        ).fetchone()
        return str(row["summary"]) if row else ""


@dataclass(frozen=True, slots=True)
class URLCacheRepository:
    connection: sqlite3.Connection
    installation_id: str
    tenant_id: str

    def get(self, url: str) -> dict | None:
        if not url:
            return None
        row = self.connection.execute(
            """SELECT * FROM url_cache
            WHERE installation_id=? AND tenant_id=? AND url=?""",
            (self.installation_id, self.tenant_id, url),
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        if value.get("expires_at", 0) < time.time():
            return None
        try:
            value["metadata"] = json.loads(value.get("metadata") or "{}")
        except json.JSONDecodeError:
            value["metadata"] = {}
        value["cached"] = True
        return value


class TaskRepository:
    """CRUD repository for the task-tracking system."""

    def __init__(self, connection: sqlite3.Connection, installation_id: str, tenant_id: str) -> None:
        self._conn = connection
        self._iid = installation_id
        self._tid = tenant_id

    def create(self, task: dict) -> dict:
        now = time.time()
        scope_id = str(task.get("scope_id") or "")
        if not scope_id:
            raise ValueError("scope_id is required")
        execution_key = str(task.get("execution_key") or "")
        if execution_key:
            existing = self.get_by_execution_key(execution_key, scope_id=scope_id)
            if existing:
                return existing
        task_id = str(task.get("id") or f"task_{uuid.uuid4().hex}")
        try:
            self._conn.execute(
                """INSERT INTO tasks
                   (id, installation_id, tenant_id, title, description, type, status,
                    assignee_id, creator_id, channel_id, scope_id, source, external_id,
                    start_time, due_time, priority, created_at, updated_at, execution_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, self._iid, self._tid,
                    task["title"], task.get("description", ""),
                    task.get("type", "task"), task.get("status", "pending"),
                    task.get("assignee_id", ""), task.get("creator_id", ""),
                    task.get("channel_id", ""), scope_id,
                    task.get("source", "manual"), task.get("external_id", ""),
                    task.get("start_time", 0), task.get("due_time", 0),
                    task.get("priority", 1), now, now, execution_key,
                ),
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            if execution_key:
                existing = self.get_by_execution_key(execution_key, scope_id=scope_id)
                if existing:
                    return existing
            raise
        self._conn.commit()
        return self.get(task_id, scope_id=scope_id)  # type: ignore[return-value]

    def get(self, task_id: str, *, scope_id: str) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM tasks
            WHERE installation_id=? AND tenant_id=? AND scope_id=? AND id=?""",
            (self._iid, self._tid, scope_id, task_id),
        ).fetchone()
        return dict(row) if row else None

    def get_by_execution_key(self, execution_key: str, *, scope_id: str) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM tasks
            WHERE installation_id=? AND tenant_id=? AND scope_id=? AND execution_key=?""",
            (self._iid, self._tid, scope_id, execution_key),
        ).fetchone()
        return dict(row) if row else None

    def list(self, *, assignee_id: str = "", status: str = "", task_type: str = "",
             scope_id: str, due_before: float = 0, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE installation_id=? AND tenant_id=? AND scope_id=?"
        params: list = [self._iid, self._tid, scope_id]
        if assignee_id:
            sql += " AND assignee_id=?"
            params.append(assignee_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if task_type:
            sql += " AND type=?"
            params.append(task_type)
        if due_before:
            sql += " AND due_time > 0 AND due_time <= ?"
            params.append(due_before)
        sql += " ORDER BY due_time ASC, priority DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update(self, task_id: str, updates: dict, *, scope_id: str) -> dict | None:
        existing = self.get(task_id, scope_id=scope_id)
        if not existing:
            return None
        allowed = {
            "title", "description", "type", "status", "assignee_id",
            "start_time", "due_time", "priority",
        }
        sets: list[str] = []
        params: list = []
        for key, value in updates.items():
            if key in allowed and value is not None:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return existing
        sets.append("updated_at=?")
        params.append(time.time())
        self._conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            "WHERE installation_id=? AND tenant_id=? AND scope_id=? AND id=?",
            [*params, self._iid, self._tid, scope_id, task_id],  # type: ignore[arg-type]
        )
        self._conn.commit()
        return self.get(task_id, scope_id=scope_id)

    def overview(self, *, scope_id: str) -> dict:
        base = "WHERE installation_id=? AND tenant_id=? AND scope_id=?"
        params: list = [self._iid, self._tid, scope_id]
        rows = self._conn.execute(
            f"SELECT status, COUNT(*) as cnt FROM tasks {base} GROUP BY status",  # noqa: S608
            params,
        ).fetchall()
        return {dict(row)["status"]: dict(row)["cnt"] for row in rows}


@dataclass(frozen=True, slots=True)
class MemoryRepositories:
    messages: MessageRepository
    profiles: ProfileRepository
    knowledge: KnowledgeRepository
    summaries: SummaryRepository
    urls: URLCacheRepository
    tasks: TaskRepository

    @classmethod
    def create(
        cls,
        connection: sqlite3.Connection,
        installation_id: str = "default",
        tenant_id: str = "default",
    ) -> MemoryRepositories:
        return cls(
            MessageRepository(connection, installation_id, tenant_id),
            ProfileRepository(connection, installation_id, tenant_id),
            KnowledgeRepository(connection, installation_id, tenant_id),
            SummaryRepository(connection, installation_id, tenant_id),
            URLCacheRepository(connection, installation_id, tenant_id),
            TaskRepository(connection, installation_id, tenant_id),
        )
