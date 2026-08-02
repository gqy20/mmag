"""Focused repositories extracted from the original Memory facade."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


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


@dataclass(frozen=True, slots=True)
class MemoryRepositories:
    messages: MessageRepository
    profiles: ProfileRepository
    knowledge: KnowledgeRepository
    summaries: SummaryRepository
    urls: URLCacheRepository

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
        )
