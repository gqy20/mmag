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


_CONTROL_PLANE_SQL = (
    """CREATE TABLE IF NOT EXISTS inbox_events (
        event_id TEXT PRIMARY KEY, platform TEXT NOT NULL, event_type TEXT NOT NULL,
        conversation_id TEXT NOT NULL, actor_id TEXT NOT NULL, occurred_at REAL NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'accepted', version INTEGER NOT NULL DEFAULT 0,
        received_at REAL NOT NULL, updated_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_events(status, received_at)",
    """CREATE TABLE IF NOT EXISTS outbox_deliveries (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, channel_id TEXT NOT NULL,
        message TEXT NOT NULL, props TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', remote_id TEXT NOT NULL DEFAULT '',
        agent_run_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox_deliveries(status, next_attempt_at)",
    """CREATE TABLE IF NOT EXISTS lifecycle_entities (
        entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(entity_type, entity_id)
    )""",
    """CREATE TABLE IF NOT EXISTS state_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, from_state TEXT NOT NULL,
        to_state TEXT NOT NULL, version INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
        actor_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        FOREIGN KEY(entity_type, entity_id) REFERENCES lifecycle_entities(entity_type, entity_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transition_entity ON state_transitions(entity_type, entity_id, version)",
    """CREATE TABLE IF NOT EXISTS scopes (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL DEFAULT '', project_id TEXT NOT NULL DEFAULT '',
        customer_id TEXT NOT NULL DEFAULT '', conversation_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS enterprise_entities (
        entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, scope_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 1.0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(entity_type, entity_id), FOREIGN KEY(scope_id) REFERENCES scopes(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_enterprise_scope ON enterprise_entities(scope_id, entity_type)",
    """CREATE TABLE IF NOT EXISTS approval_requests (
        id TEXT PRIMARY KEY, capability_name TEXT NOT NULL, arguments TEXT NOT NULL,
        resume_token TEXT NOT NULL UNIQUE, requested_by TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '',
        expires_at REAL, decided_by TEXT NOT NULL DEFAULT '', decision_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        id TEXT PRIMARY KEY, event_type TEXT NOT NULL, actor_id TEXT NOT NULL DEFAULT '',
        scope_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL DEFAULT '',
        target TEXT NOT NULL DEFAULT '', decision TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_scope_time ON audit_events(scope_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS quota_usage (
        subject_id TEXT NOT NULL, period TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL, PRIMARY KEY(subject_id, period)
    )""",
)


def _v004_add_control_plane(connection: sqlite3.Connection) -> None:
    for statement in _CONTROL_PLANE_SQL:
        connection.execute(statement)


def _v005_add_inbox_retry_state(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(inbox_events)")}
    if "attempts" not in columns:
        connection.execute(
            "ALTER TABLE inbox_events ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "next_attempt_at" not in columns:
        connection.execute(
            "ALTER TABLE inbox_events ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_retry ON inbox_events(status, next_attempt_at)"
    )


def _v006_add_delivery_presentation(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(outbox_deliveries)")
    }
    additions = {
        "root_id": "TEXT NOT NULL DEFAULT ''",
        "message_kind": "TEXT NOT NULL DEFAULT 'result'",
        "scope_id": "TEXT NOT NULL DEFAULT ''",
        "artifact_refs": "TEXT NOT NULL DEFAULT '[]'",
        "file_ids": "TEXT NOT NULL DEFAULT '[]'",
        "actions": "TEXT NOT NULL DEFAULT '[]'",
        "update_post_id": "TEXT NOT NULL DEFAULT ''",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE outbox_deliveries ADD COLUMN {name} {declaration}"
            )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_idempotency "
        "ON outbox_deliveries(idempotency_key) WHERE idempotency_key != ''"
    )


def _v007_add_action_tokens(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS action_tokens (
        jti TEXT PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL,
        scope_id TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
        expires_at REAL NOT NULL, used_at REAL NOT NULL DEFAULT 0,
        used_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_expiry ON action_tokens(expires_at, used_at)"
    )


def _v008_add_run_governance(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS quota_reservations (
        reservation_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, period TEXT NOT NULL,
        run_id TEXT NOT NULL, reserved_cost_usd REAL NOT NULL,
        actual_cost_usd REAL NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'reserved', expires_at REAL NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_reservation_subject "
        "ON quota_reservations(subject_id, period, status)"
    )


def _v009_add_package_releases(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS package_releases (
        id TEXT PRIMARY KEY, package_kind TEXT NOT NULL, package_name TEXT NOT NULL,
        package_version TEXT NOT NULL, package_hash TEXT NOT NULL,
        eval_hash TEXT NOT NULL DEFAULT '', gate_version TEXT NOT NULL,
        checks TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL, released_by TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(package_kind, package_hash)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_package_release_active "
        "ON package_releases(package_kind, package_name, status)"
    )


def _v010_add_tenant_isolation(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE user_profiles RENAME TO user_profiles_v009")
    connection.execute(
        """CREATE TABLE user_profiles (
        installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
        username TEXT, first_seen REAL, message_count INTEGER DEFAULT 0,
        expertise TEXT, preferences TEXT, style TEXT DEFAULT 'casual', notes TEXT,
        last_interaction REAL, topics TEXT, active_hours TEXT,
        _question_count INTEGER DEFAULT 0, is_bot INTEGER DEFAULT 0,
        PRIMARY KEY(installation_id, tenant_id, user_id)
        )"""
    )
    connection.execute(
        """INSERT INTO user_profiles
        (installation_id, tenant_id, user_id, username, first_seen, message_count,
         expertise, preferences, style, notes, last_interaction, topics, active_hours,
         _question_count, is_bot)
        SELECT 'default', 'default', user_id, username, first_seen, message_count,
         expertise, preferences, style, notes, last_interaction, topics, active_hours,
         _question_count, is_bot
        FROM user_profiles_v009"""
    )
    connection.execute("DROP TABLE user_profiles_v009")
    connection.execute(
        "CREATE INDEX idx_user_profiles_actor ON user_profiles(user_id)"
    )

    connection.execute("ALTER TABLE url_cache RENAME TO url_cache_v009")
    connection.execute(
        """CREATE TABLE url_cache (
        installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        url TEXT NOT NULL, url_hash TEXT NOT NULL, kind TEXT, status TEXT,
        title TEXT, summary TEXT, content TEXT, metadata TEXT, fetched_at REAL,
        expires_at REAL, error TEXT,
        PRIMARY KEY(installation_id, tenant_id, url)
        )"""
    )
    connection.execute(
        """INSERT INTO url_cache
        (installation_id, tenant_id, url, url_hash, kind, status, title, summary,
         content, metadata, fetched_at, expires_at, error)
        SELECT 'default', 'default', url, url_hash, kind, status, title, summary,
         content, metadata, fetched_at, expires_at, error
        FROM url_cache_v009"""
    )
    connection.execute("DROP TABLE url_cache_v009")

    additions = {
        "message_log": {
            "installation_id": "TEXT NOT NULL DEFAULT 'default'",
            "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
            "scope_id": "TEXT NOT NULL DEFAULT ''",
        },
        "team_knowledge": {
            "installation_id": "TEXT NOT NULL DEFAULT 'default'",
            "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        },
        "conversation_segments": {
            "installation_id": "TEXT NOT NULL DEFAULT 'default'",
            "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        },
        "open_items": {
            "installation_id": "TEXT NOT NULL DEFAULT 'default'",
            "tenant_id": "TEXT NOT NULL DEFAULT 'default'",
        },
        "scopes": {
            "platform": "TEXT NOT NULL DEFAULT ''",
            "installation_id": "TEXT NOT NULL DEFAULT ''",
            "tenant_id": "TEXT NOT NULL DEFAULT ''",
            "kind": "TEXT NOT NULL DEFAULT 'channel'",
            "owner_id": "TEXT NOT NULL DEFAULT ''",
            "team_id": "TEXT NOT NULL DEFAULT ''",
            "channel_type": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in additions.items():
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    connection.execute(
        "CREATE INDEX idx_message_namespace_channel "
        "ON message_log(installation_id, tenant_id, channel_id, create_at DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_knowledge_namespace_channel "
        "ON team_knowledge(installation_id, tenant_id, channel_id, updated_at DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_segments_namespace_channel "
        "ON conversation_segments(installation_id, tenant_id, channel_id, started_at DESC)"
    )


def _v011_purge_pre_isolation_memory(connection: sqlite3.Connection) -> None:
    """Discard memory whose actor and tenant provenance cannot be trusted.

    Control-plane runs, approvals, artifacts, package releases, and audit records are
    intentionally retained. Only conversational memory derived before strict scope
    isolation is removed.
    """
    connection.execute("DELETE FROM message_log_fts")
    connection.execute("DELETE FROM message_log_fts_map")
    connection.execute("DELETE FROM message_log")
    connection.execute("DELETE FROM team_knowledge_fts")
    connection.execute("DELETE FROM team_knowledge")
    connection.execute("DELETE FROM conversation_segments")
    connection.execute("DELETE FROM open_items")
    connection.execute("DELETE FROM user_profiles")
    connection.execute("DELETE FROM url_cache")


def _v012_add_delivery_actor(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(outbox_deliveries)")
    }
    if "actor_id" not in columns:
        connection.execute(
            "ALTER TABLE outbox_deliveries ADD COLUMN actor_id TEXT NOT NULL DEFAULT ''"
        )


def _v013_add_personal_skills(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE personal_skills (
        id TEXT NOT NULL, revision INTEGER NOT NULL,
        installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,
        name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        base_skill_ref TEXT NOT NULL, preferred_agent TEXT NOT NULL DEFAULT '',
        activation_intents TEXT NOT NULL DEFAULT '[]',
        activation_keywords TEXT NOT NULL DEFAULT '[]',
        auto_select INTEGER NOT NULL DEFAULT 0,
        instruction TEXT NOT NULL, template TEXT NOT NULL DEFAULT '',
        sha256 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(id, revision)
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_personal_skills_owner ON personal_skills"
        "(installation_id, tenant_id, owner_id, status, updated_at DESC)"
    )


def _v014_add_work_cases_and_interactions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE work_cases (
        id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, goal TEXT NOT NULL,
        result_summary TEXT NOT NULL, agent_name TEXT NOT NULL DEFAULT '',
        skill_ref TEXT NOT NULL DEFAULT '', personal_skill_ref TEXT NOT NULL DEFAULT '',
        artifact_refs TEXT NOT NULL DEFAULT '[]', feedback TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'candidate', created_at REAL NOT NULL,
        updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_work_cases_owner ON work_cases"
        "(installation_id, tenant_id, owner_id, status, updated_at DESC)"
    )
    connection.execute(
        """CREATE TABLE interaction_sessions (
        id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', expires_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_interaction_open ON interaction_sessions"
        "(installation_id, tenant_id, owner_id, conversation_id, status, updated_at DESC)"
    )


def _v015_add_personal_workflow_provenance(connection: sqlite3.Connection) -> None:
    additions = {
        "source_run_id": "TEXT NOT NULL DEFAULT ''",
        "source_message_id": "TEXT NOT NULL DEFAULT ''",
        "goal_hash": "TEXT NOT NULL DEFAULT ''",
        "result_hash": "TEXT NOT NULL DEFAULT ''",
        "provenance": "TEXT NOT NULL DEFAULT '{}'",
    }
    existing = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(work_cases)")
    }
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE work_cases ADD COLUMN {name} {declaration}")
    skill_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(personal_skills)")
    }
    if "source_case_ids" not in skill_columns:
        connection.execute(
            "ALTER TABLE personal_skills ADD COLUMN source_case_ids TEXT NOT NULL DEFAULT '[]'"
        )
    connection.execute(
        "CREATE INDEX idx_work_case_source_run ON work_cases(owner_id, source_run_id)"
    )


def _v016_add_memory_items(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE memory_items (
        id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, kind TEXT NOT NULL,
        content TEXT NOT NULL, content_hash TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active', expires_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX idx_memory_active_content ON memory_items
        (installation_id, tenant_id, owner_id, content_hash) WHERE status='active'"""
    )
    connection.execute(
        """CREATE INDEX idx_memory_owner ON memory_items
        (installation_id, tenant_id, owner_id, status, updated_at DESC)"""
    )
    connection.execute(
        """CREATE TABLE memory_item_sources (
        memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
        source_type TEXT NOT NULL, source_ref TEXT NOT NULL, source_scope_id TEXT NOT NULL,
        PRIMARY KEY (memory_id, source_type, source_ref)
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_memory_source ON memory_item_sources(source_type, source_ref)"
    )
    connection.execute(
        """CREATE VIRTUAL TABLE memory_items_fts USING fts5(
        content, tokenize='unicode61 remove_diacritics 2'
        )"""
    )
    connection.execute(
        """CREATE TABLE memory_items_fts_map (
        rowid INTEGER PRIMARY KEY, memory_id TEXT UNIQUE NOT NULL
        REFERENCES memory_items(id) ON DELETE CASCADE
        )"""
    )


def _v017_add_digital_personas(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE digital_personas (
        id TEXT NOT NULL, revision INTEGER NOT NULL, installation_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, owner_username TEXT NOT NULL,
        scope_id TEXT NOT NULL, display_name TEXT NOT NULL,
        allowed_topics TEXT NOT NULL DEFAULT '[]', denied_topics TEXT NOT NULL DEFAULT '[]',
        response_mode TEXT NOT NULL DEFAULT 'auto',
        source_memory_ids TEXT NOT NULL DEFAULT '[]',
        published_snapshots TEXT NOT NULL DEFAULT '[]', sha256 TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft', created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY (id, revision)
        )"""
    )
    connection.execute(
        """CREATE INDEX idx_persona_owner ON digital_personas
        (installation_id, tenant_id, owner_id, updated_at DESC)"""
    )
    connection.execute(
        """CREATE INDEX idx_persona_active ON digital_personas
        (installation_id, tenant_id, status, owner_username)"""
    )


def _v018_add_persona_reply_approval(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE digital_personas ADD COLUMN approval_topics TEXT NOT NULL DEFAULT '[]'"
    )
    connection.execute(
        "UPDATE digital_personas SET response_mode='risk_approval' WHERE response_mode='auto'"
    )
    connection.execute(
        """CREATE TABLE persona_reply_requests (
        id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        persona_ref TEXT NOT NULL, persona_hash TEXT NOT NULL, owner_id TEXT NOT NULL,
        requester_id TEXT NOT NULL, requester_username TEXT NOT NULL,
        source_scope_id TEXT NOT NULL, source_channel_id TEXT NOT NULL,
        source_root_id TEXT NOT NULL, source_status_post_id TEXT NOT NULL DEFAULT '',
        question TEXT NOT NULL, draft_text TEXT NOT NULL, approval_reason TEXT NOT NULL,
        expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        decision_by TEXT NOT NULL DEFAULT '', decided_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX idx_persona_reply_owner ON persona_reply_requests
        (installation_id, tenant_id, owner_id, state, created_at DESC)"""
    )
    connection.execute(
        """CREATE INDEX idx_persona_reply_source ON persona_reply_requests
        (source_channel_id, source_root_id, state)"""
    )


def _v019_add_persona_approval_post(connection: sqlite3.Connection) -> None:
    connection.execute(
        """ALTER TABLE persona_reply_requests
        ADD COLUMN owner_approval_post_id TEXT NOT NULL DEFAULT ''"""
    )


def _v020_add_persona_approval_channel(connection: sqlite3.Connection) -> None:
    connection.execute(
        """ALTER TABLE persona_reply_requests
        ADD COLUMN owner_channel_id TEXT NOT NULL DEFAULT ''"""
    )


_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'task',
    status TEXT NOT NULL DEFAULT 'pending',
    assignee_id TEXT NOT NULL DEFAULT '',
    creator_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    scope_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    external_id TEXT NOT NULL DEFAULT '',
    start_time REAL NOT NULL DEFAULT 0,
    due_time REAL NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


def _v021_add_tasks(connection: sqlite3.Connection) -> None:
    for statement in _TASKS_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_assignee "
        "ON tasks(installation_id, tenant_id, assignee_id, status, due_time)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_channel "
        "ON tasks(installation_id, tenant_id, channel_id, status, due_time)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status "
        "ON tasks(installation_id, tenant_id, status, due_time)"
    )


def _v022_add_scoped_task_idempotency(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE tasks ADD COLUMN execution_key TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_execution_key "
        "ON tasks(installation_id, tenant_id, execution_key) WHERE execution_key <> ''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_scope "
        "ON tasks(installation_id, tenant_id, scope_id, status, due_time)"
    )


def _v023_add_task_drafts(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE task_drafts (
            id TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            root_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            title TEXT NOT NULL,
            items TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'draft',
            decided_by TEXT NOT NULL DEFAULT '',
            task_ids TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(installation_id, tenant_id, run_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_task_drafts_scope_state "
        "ON task_drafts(installation_id, tenant_id, scope_id, state, created_at)"
    )


def _v024_add_agent_run_identity_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE UNIQUE INDEX idx_agent_runs_execution_key "
        "ON lifecycle_entities(json_extract(payload, '$.execution_key')) "
        "WHERE entity_type='agent_run' "
        "AND coalesce(json_extract(payload, '$.execution_key'), '') <> ''"
    )
    connection.execute(
        "CREATE INDEX idx_agent_runs_parent "
        "ON lifecycle_entities(json_extract(payload, '$.parent_run_id')) "
        "WHERE entity_type='agent_run'"
    )
    connection.execute(
        "CREATE INDEX idx_agent_runs_workflow "
        "ON lifecycle_entities(json_extract(payload, '$.workflow_id')) "
        "WHERE entity_type='agent_run'"
    )


def _v025_add_capability_call_identity_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE UNIQUE INDEX idx_capability_calls_execution_key "
        "ON lifecycle_entities(json_extract(payload, '$.execution_key')) "
        "WHERE entity_type='capability_call' "
        "AND coalesce(json_extract(payload, '$.execution_key'), '') <> ''"
    )
    connection.execute(
        "CREATE INDEX idx_capability_calls_run "
        "ON lifecycle_entities(json_extract(payload, '$.run_id')) "
        "WHERE entity_type='capability_call'"
    )
    connection.execute(
        "CREATE INDEX idx_capability_calls_workflow "
        "ON lifecycle_entities(json_extract(payload, '$.workflow_id')) "
        "WHERE entity_type='capability_call'"
    )


def _v026_add_goals(connection: sqlite3.Connection) -> None:
    # The former "okr" task type was only a label, not a Goal model. Preserve
    # those work items while removing the misleading product semantics.
    connection.execute("UPDATE tasks SET type='task' WHERE type='okr'")
    connection.execute(
        """CREATE TABLE goals (
            id TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            owner_id TEXT NOT NULL DEFAULT '',
            creator_id TEXT NOT NULL,
            start_time REAL NOT NULL DEFAULT 0,
            due_time REAL NOT NULL DEFAULT 0,
            success_criteria TEXT NOT NULL DEFAULT '[]',
            source_refs TEXT NOT NULL DEFAULT '[]',
            execution_key TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE UNIQUE INDEX idx_goals_execution_key "
        "ON goals(installation_id, tenant_id, execution_key) WHERE execution_key <> ''"
    )
    connection.execute(
        "CREATE INDEX idx_goals_scope_status "
        "ON goals(installation_id, tenant_id, scope_id, status, due_time)"
    )
    connection.execute("ALTER TABLE tasks ADD COLUMN goal_id TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "CREATE INDEX idx_tasks_goal "
        "ON tasks(installation_id, tenant_id, scope_id, goal_id, status)"
    )


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
    Migration(
        version=4,
        name="add durable control plane",
        checksum=_checksum("v004-durable-control-plane-20260731"),
        upgrade=_v004_add_control_plane,
    ),
    Migration(
        version=5,
        name="add durable inbox retries",
        checksum=_checksum("v005-durable-inbox-retries-20260801"),
        upgrade=_v005_add_inbox_retry_state,
    ),
    Migration(
        version=6,
        name="add delivery presentation contract",
        checksum=_checksum("v006-add-delivery-presentation-20260801"),
        upgrade=_v006_add_delivery_presentation,
    ),
    Migration(
        version=7,
        name="add one-time action tokens",
        checksum=_checksum("v007-add-one-time-action-tokens-20260801"),
        upgrade=_v007_add_action_tokens,
    ),
    Migration(
        version=8,
        name="add durable run governance",
        checksum=_checksum("v008-add-durable-run-governance-20260802"),
        upgrade=_v008_add_run_governance,
    ),
    Migration(
        version=9,
        name="add package release records",
        checksum=_checksum("v009-add-package-release-records-20260802"),
        upgrade=_v009_add_package_releases,
    ),
    Migration(
        version=10,
        name="add tenant isolation",
        checksum=_checksum("v010-add-tenant-isolation-20260802"),
        upgrade=_v010_add_tenant_isolation,
    ),
    Migration(
        version=11,
        name="purge pre-isolation memory",
        checksum=_checksum("v011-purge-pre-isolation-memory-20260802"),
        upgrade=_v011_purge_pre_isolation_memory,
    ),
    Migration(
        version=12,
        name="add delivery actor",
        checksum=_checksum("v012-add-delivery-actor-20260802"),
        upgrade=_v012_add_delivery_actor,
    ),
    Migration(
        version=13,
        name="add personal skills",
        checksum=_checksum("v013-add-personal-skills-20260802"),
        upgrade=_v013_add_personal_skills,
    ),
    Migration(
        version=14,
        name="add work cases and interactions",
        checksum=_checksum("v014-add-work-cases-and-interactions-20260802"),
        upgrade=_v014_add_work_cases_and_interactions,
    ),
    Migration(
        version=15,
        name="add personal workflow provenance",
        checksum=_checksum("v015-add-personal-workflow-provenance-20260802"),
        upgrade=_v015_add_personal_workflow_provenance,
    ),
    Migration(
        version=16,
        name="add governed memory items",
        checksum=_checksum("v016-add-governed-memory-items-20260802"),
        upgrade=_v016_add_memory_items,
    ),
    Migration(
        version=17,
        name="add digital personas",
        checksum=_checksum("v017-add-digital-personas-20260802"),
        upgrade=_v017_add_digital_personas,
    ),
    Migration(
        version=18,
        name="add persona reply approval",
        checksum=_checksum("v018-add-persona-reply-approval-20260802"),
        upgrade=_v018_add_persona_reply_approval,
    ),
    Migration(
        version=19,
        name="add persona approval post",
        checksum=_checksum("v019-add-persona-approval-post-20260802"),
        upgrade=_v019_add_persona_approval_post,
    ),
    Migration(
        version=20,
        name="add persona approval channel",
        checksum=_checksum("v020-add-persona-approval-channel-20260803"),
        upgrade=_v020_add_persona_approval_channel,
    ),
    Migration(
        version=21,
        name="add task tracking",
        checksum=_checksum("v021-add-task-tracking-20260808"),
        upgrade=_v021_add_tasks,
    ),
    Migration(
        version=22,
        name="add scoped task idempotency",
        checksum=_checksum("v022-add-scoped-task-idempotency-20260809"),
        upgrade=_v022_add_scoped_task_idempotency,
    ),
    Migration(
        version=23,
        name="add meeting task drafts",
        checksum=_checksum("v023-add-meeting-task-drafts-20260814"),
        upgrade=_v023_add_task_drafts,
    ),
    Migration(
        version=24,
        name="add durable agent run identity",
        checksum=_checksum("v024-add-durable-agent-run-identity-20260815"),
        upgrade=_v024_add_agent_run_identity_indexes,
    ),
    Migration(
        version=25,
        name="add durable capability call identity",
        checksum=_checksum("v025-add-durable-capability-call-identity-20260816"),
        upgrade=_v025_add_capability_call_identity_indexes,
    ),
    Migration(
        version=26,
        name="add simple goals",
        checksum=_checksum("v026-add-simple-goals-20260816"),
        upgrade=_v026_add_goals,
    ),
)

LATEST_SCHEMA_VERSION = DEFAULT_MIGRATIONS[-1].version
