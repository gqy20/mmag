"""SQLite schema migration 契约测试。"""

import sqlite3

import pytest

from mmag.infrastructure.sqlite import (
    DEFAULT_MIGRATIONS,
    LATEST_SCHEMA_VERSION,
    FutureSchemaError,
    Migration,
    MigrationError,
    SQLiteDatabase,
    apply_migrations,
)
from mmag.memory import Memory


def _memory_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _migration_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()


def _create_legacy_user_profiles(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE user_profiles (
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
            _question_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute("INSERT INTO user_profiles(user_id, username) VALUES ('u1', 'alice')")
    conn.commit()


def _create_legacy_message_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE message_cache (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            user_id TEXT,
            username TEXT,
            message TEXT,
            create_at REAL,
            post_type TEXT DEFAULT '',
            root_id TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        INSERT INTO message_cache
            (id, channel_id, user_id, username, message, create_at, post_type, root_id)
        VALUES ('p1', 'ch1', 'u1', 'alice', '部署流程', 123.0, '', '')
        """
    )
    conn.commit()


def test_fresh_database_migrates_to_latest_schema():
    database = SQLiteDatabase(":memory:")
    conn = database.connect()

    versions = [row["version"] for row in _migration_rows(conn)]
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    assert versions == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert {"message_log", "user_profiles", "team_knowledge", "url_cache"} <= tables
    inbox_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(inbox_events)").fetchall()
    }
    assert {"attempts", "next_attempt_at"} <= inbox_columns
    profile_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)").fetchall()
    }
    assert {"installation_id", "tenant_id", "user_id"} <= profile_columns
    task_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    task_indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(tasks)").fetchall()
    }
    assert "execution_key" in task_columns
    assert {"idx_tasks_execution_key", "idx_tasks_scope"} <= task_indexes
    conn.close()


def test_existing_database_purges_unscoped_profiles():
    conn = _memory_connection()
    _create_legacy_user_profiles(conn)

    apply_migrations(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    profile = conn.execute(
        "SELECT user_id, username, is_bot FROM user_profiles WHERE user_id='u1'"
    ).fetchone()
    assert "is_bot" in columns
    assert profile is None


def test_legacy_message_cache_is_removed_with_unscoped_data():
    conn = _memory_connection()
    _create_legacy_message_cache(conn)

    apply_migrations(conn)

    message = conn.execute("SELECT * FROM message_log WHERE id='p1'").fetchone()
    fts = conn.execute(
        """
        SELECT f.message
        FROM message_log_fts f
        JOIN message_log_fts_map m ON m.rowid = f.rowid
        WHERE m.message_id='p1'
        """
    ).fetchone()
    old_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_cache'"
    ).fetchone()

    assert message is None
    assert fts is None
    assert old_table is None


def test_migrations_are_idempotent():
    conn = _memory_connection()

    apply_migrations(conn)
    first_run = [tuple(row) for row in _migration_rows(conn)]
    apply_migrations(conn)

    assert [tuple(row) for row in _migration_rows(conn)] == first_run


def test_failed_migration_rolls_back_schema_and_version_record():
    conn = _memory_connection()
    apply_migrations(conn)

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)")
        raise RuntimeError("boom")

    failing = Migration(
        version=LATEST_SCHEMA_VERSION + 1,
        name="failing migration",
        checksum="test-failing-v1",
        upgrade=fail_after_ddl,
    )

    with pytest.raises(MigrationError, match="failing migration"):
        apply_migrations(conn, (*DEFAULT_MIGRATIONS, failing))

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
    ).fetchone()
    applied = {row["version"] for row in _migration_rows(conn)}
    assert table is None
    assert failing.version not in applied


def test_future_database_version_fails_fast():
    conn = _memory_connection()
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES (?, 'future', 'future-v1', 0)",
        (LATEST_SCHEMA_VERSION + 100,),
    )
    conn.commit()

    with pytest.raises(FutureSchemaError, match="newer than supported"):
        apply_migrations(conn)


def test_memory_opens_and_purges_unscoped_legacy_database():
    path = "file:mmag-legacy-integration?mode=memory&cache=shared"
    legacy = sqlite3.connect(path, uri=True)
    _create_legacy_message_cache(legacy)

    memory = Memory(path)
    legacy.close()
    messages = memory.get_recent_messages("ch1")
    verification = sqlite3.connect(path, uri=True)
    version = verification.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    verification.close()
    memory.close()

    assert messages == []
    assert version == LATEST_SCHEMA_VERSION
