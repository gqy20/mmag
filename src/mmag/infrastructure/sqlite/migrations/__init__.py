"""Ordered mmag SQLite schema migrations.

Applied migrations are immutable. Add a new version instead of editing an existing one.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ..fts import cjk_tokenize_for_fts
from ..migration import Migration

if TYPE_CHECKING:
    import sqlite3

_INITIAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversation_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    started_at REAL,
    ended_at REAL,
    topic TEXT,
    summary TEXT,
    participants TEXT,
    key_points TEXT,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS open_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT,
    description TEXT,
    mentioned_by TEXT,
    created_at REAL,
    resolved_at REAL,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    username TEXT,
    first_seen REAL,
    message_count INTEGER DEFAULT 0,
    expertise TEXT,
    preferences TEXT,
    style TEXT DEFAULT 'casual',
    notes TEXT,
    last_interaction REAL,
    topics TEXT,
    active_hours TEXT,
    _question_count INTEGER DEFAULT 0,
    is_bot INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT,
    key TEXT,
    value TEXT,
    source TEXT DEFAULT 'conversation',
    confidence REAL DEFAULT 0.5,
    updated_at REAL,
    mentioned_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS team_knowledge_fts USING fts5(
    key,
    value,
    content='team_knowledge',
    content_rowid='id'
);

CREATE TABLE IF NOT EXISTS message_log (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    user_id TEXT,
    username TEXT,
    message TEXT,
    create_at REAL,
    post_type TEXT DEFAULT '',
    root_id TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_message_log_ch_time
ON message_log(channel_id, create_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS message_log_fts USING fts5(
    message,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS message_log_fts_map (
    rowid INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    FOREIGN KEY (message_id) REFERENCES message_log(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS url_cache (
    url TEXT PRIMARY KEY,
    url_hash TEXT NOT NULL,
    kind TEXT,
    status TEXT,
    title TEXT,
    summary TEXT,
    content TEXT,
    metadata TEXT,
    fetched_at REAL,
    expires_at REAL,
    error TEXT
);
"""


def _checksum(revision: str) -> str:
    return hashlib.sha256(revision.encode()).hexdigest()


def _v001_initial_schema(connection: sqlite3.Connection) -> None:
    for statement in _INITIAL_SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


def _v002_add_profile_is_bot(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_profiles)")}
    if "is_bot" not in columns:
        connection.execute("ALTER TABLE user_profiles ADD COLUMN is_bot INTEGER DEFAULT 0")


def _v003_migrate_message_cache(connection: sqlite3.Connection) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_cache'"
    ).fetchone()
    if not exists:
        return

    connection.execute(
        """
        INSERT OR IGNORE INTO message_log
            (id, channel_id, user_id, username, message, create_at, post_type, root_id)
        SELECT id, channel_id, user_id, username, message, create_at, post_type, root_id
        FROM message_cache
        """
    )

    rows = connection.execute(
        """
        SELECT log.id, log.message
        FROM message_log log
        LEFT JOIN message_log_fts_map map ON map.message_id = log.id
        WHERE map.message_id IS NULL
        """
    ).fetchall()
    for row in rows:
        connection.execute(
            "INSERT INTO message_log_fts(message) VALUES (?)",
            (cjk_tokenize_for_fts(str(row[1] or "")),),
        )
        fts_rowid = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO message_log_fts_map(rowid, message_id) VALUES (?, ?)",
            (fts_rowid, row[0]),
        )

    missing = connection.execute(
        """
        SELECT COUNT(*)
        FROM message_cache cache
        LEFT JOIN message_log log ON log.id = cache.id
        WHERE log.id IS NULL
        """
    ).fetchone()[0]
    if missing:
        raise RuntimeError(f"message_cache migration left {missing} rows behind")
    connection.execute("DROP TABLE message_cache")


DEFAULT_MIGRATIONS = (
    Migration(
        version=1,
        name="initial schema",
        checksum=_checksum("v001-initial-schema-20260731"),
        upgrade=_v001_initial_schema,
    ),
    Migration(
        version=2,
        name="add user profile is_bot",
        checksum=_checksum("v002-add-profile-is-bot-20260731"),
        upgrade=_v002_add_profile_is_bot,
    ),
    Migration(
        version=3,
        name="migrate legacy message cache",
        checksum=_checksum("v003-migrate-message-cache-20260731"),
        upgrade=_v003_migrate_message_cache,
    ),
)

LATEST_SCHEMA_VERSION = DEFAULT_MIGRATIONS[-1].version
