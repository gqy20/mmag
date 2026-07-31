"""Focused repositories extracted from the legacy Memory facade."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True, slots=True)
class MessageRepository:
    connection: sqlite3.Connection

    def contains(self, message_id: str) -> bool:
        if not message_id:
            return False
        row = self.connection.execute(
            "SELECT 1 FROM message_log WHERE id=?", (message_id,)
        ).fetchone()
        return row is not None

    def recent(self, conversation_id: str, limit: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM message_log WHERE channel_id=? ORDER BY create_at DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def actor_for(self, message_id: str) -> str | None:
        if not message_id:
            return None
        row = self.connection.execute(
            "SELECT user_id FROM message_log WHERE id=?", (message_id,)
        ).fetchone()
        return str(row["user_id"]) if row else None

    def latest_timestamp(self, conversation_id: str) -> float:
        row = self.connection.execute(
            "SELECT MAX(create_at) AS ts FROM message_log WHERE channel_id=?",
            (conversation_id,),
        ).fetchone()
        return float(row["ts"]) if row and row["ts"] else 0.0


@dataclass(frozen=True, slots=True)
class ProfileRepository:
    connection: sqlite3.Connection

    def get(self, user_id: str, *, decode: bool = False) -> dict:
        row = self.connection.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        profile = dict(row) if row else {}
        if not profile or not decode:
            return profile
        for key, default in (("topics", []), ("active_hours", {})):
            try:
                profile[key] = json.loads(profile.get(key) or "")
            except (TypeError, json.JSONDecodeError):
                profile[key] = default
        return profile


@dataclass(frozen=True, slots=True)
class KnowledgeRepository:
    connection: sqlite3.Connection

    def recent(self, scope_id: str, limit: int = 10) -> list[dict]:
        rows = self.connection.execute(
            """SELECT * FROM team_knowledge WHERE channel_id=?
            ORDER BY updated_at DESC LIMIT ?""",
            (scope_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


@dataclass(frozen=True, slots=True)
class SummaryRepository:
    connection: sqlite3.Connection

    def latest(self, conversation_id: str) -> str:
        row = self.connection.execute(
            """SELECT summary FROM conversation_segments
            WHERE channel_id=? AND status='active'
            ORDER BY started_at DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        return str(row["summary"]) if row else ""


@dataclass(frozen=True, slots=True)
class URLCacheRepository:
    connection: sqlite3.Connection

    def get(self, url: str) -> dict | None:
        if not url:
            return None
        row = self.connection.execute("SELECT * FROM url_cache WHERE url=?", (url,)).fetchone()
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


@dataclass(frozen=True, slots=True)
class MemoryRepositories:
    messages: MessageRepository
    profiles: ProfileRepository
    knowledge: KnowledgeRepository
    summaries: SummaryRepository
    urls: URLCacheRepository

    @classmethod
    def create(cls, connection: sqlite3.Connection) -> MemoryRepositories:
        return cls(
            MessageRepository(connection),
            ProfileRepository(connection),
            KnowledgeRepository(connection),
            SummaryRepository(connection),
            URLCacheRepository(connection),
        )
