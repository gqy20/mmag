"""SQLite repositories for inbox, outbox, lifecycle, context and audit."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..infrastructure.sqlite import SQLiteDatabase
from ..logger import get_logger, log_event
from .memory_items import MemoryItemStore
from .models import (
    ApprovalRequest,
    Artifact,
    AuditEvent,
    DeliveryRecord,
    EnterpriseContext,
    EntityType,
    InboundEvent,
    InboxRecord,
    LifecycleEntity,
    OutboundMessage,
    Scope,
    StateTransition,
)
from .persona_replies import PersonaReplyStore
from .personal_skills import PersonalSkillStore
from .personas import DigitalPersonaStore
from .quota import QuotaStore
from .releases import ReleaseStore
from .runs import AgentRunStore
from .task_drafts import TaskDraftStore
from .work_cases import InteractionSessionStore, WorkCaseStore

if TYPE_CHECKING:
    from pathlib import Path


log = get_logger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class SQLiteControlPlane:
    """Single-process SQLite control plane with serialized writes."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._connection = SQLiteDatabase(path).connect()
        self._lock = threading.RLock()
        self.quota = QuotaStore(self._connection, self._lock)
        self.releases = ReleaseStore(self._connection, self._lock)
        self.runs = AgentRunStore(self._connection, self._lock)
        self.personal_skills = PersonalSkillStore(self._connection, self._lock)
        self.work_cases = WorkCaseStore(self._connection, self._lock)
        self.interactions = InteractionSessionStore(self._connection, self._lock)
        self.memory_items = MemoryItemStore(self._connection, self._lock)
        self.personas = DigitalPersonaStore(self._connection, self._lock)
        self.persona_replies = PersonaReplyStore(self._connection, self._lock)
        self.task_drafts = TaskDraftStore(self._connection, self._lock)

    def close(self) -> None:
        self._connection.close()

    def accept_event(self, event: InboundEvent) -> bool:
        now = time.time()
        with self._lock:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO inbox_events
                (event_id, platform, event_type, conversation_id, actor_id, occurred_at,
                 payload, status, received_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)""",
                (
                    event.event_id,
                    event.platform,
                    event.event_type,
                    event.conversation_id,
                    event.actor_id,
                    event.occurred_at,
                    _json(dict(event.payload)),
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def get_inbox(self, event_id: str) -> InboxRecord:
        row = self._connection.execute(
            "SELECT * FROM inbox_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._inbox_record(row)

    def recover_inbox(self) -> list[InboxRecord]:
        with self._lock:
            self._connection.execute(
                """UPDATE inbox_events SET status='accepted', next_attempt_at=0, updated_at=?
                WHERE status IN ('processing', 'retrying')""",
                (time.time(),),
            )
            self._connection.commit()
        rows = self._connection.execute(
            "SELECT * FROM inbox_events WHERE status='accepted' ORDER BY received_at"
        ).fetchall()
        return [self._inbox_record(row) for row in rows]

    def list_dead_letters(
        self,
        *,
        limit: int = 100,
        conversation_id: str | None = None,
    ) -> list[InboxRecord]:
        """Return terminal processing failures without mutating their evidence."""
        if limit < 1:
            raise ValueError("dead-letter limit must be positive")
        if conversation_id is None:
            rows = self._connection.execute(
                """SELECT * FROM inbox_events WHERE status='failed'
                ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """SELECT * FROM inbox_events
                WHERE status='failed' AND conversation_id=?
                ORDER BY updated_at DESC LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        return [self._inbox_record(row) for row in rows]

    def mark_inbox_processing(self, event_id: str) -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE inbox_events SET status='processing', attempts=attempts+1,
                next_attempt_at=0, version=version+1, updated_at=? WHERE event_id=?""",
                (time.time(), event_id),
            )
            self._connection.commit()

    def mark_inbox_retry(self, event_id: str, error: str, retry_at: float) -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE inbox_events SET status='retrying', last_error=?, next_attempt_at=?,
                version=version+1, updated_at=? WHERE event_id=?""",
                (error, retry_at, time.time(), event_id),
            )
            self._connection.commit()

    def mark_inbox_failed(self, event_id: str, error: str) -> None:
        self._update_inbox(event_id, "failed", error)

    def complete_event(
        self, event_id: str, messages: tuple[OutboundMessage, ...]
    ) -> tuple[str, ...]:
        now = time.time()
        delivery_ids: list[str] = []
        log_event(
            log,
            "outbox.transaction.waiting",
            status="running",
            delivery_count=len(messages),
        )
        with self._lock:
            log_event(
                log,
                "outbox.transaction.started",
                status="running",
                delivery_count=len(messages),
            )
            stage = "begin"
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for index, message in enumerate(messages):
                    stable_key = message.idempotency_key or f"{event_id}:{index}"
                    delivery_id = uuid.uuid5(uuid.NAMESPACE_URL, f"mmag:delivery:{stable_key}").hex
                    channel_id = message.channel_id or message.conversation_id
                    agent_run_id = message.agent_run_id or f"run:{event_id}"
                    stage = "delivery_insert"
                    self._connection.execute(
                        """INSERT INTO outbox_deliveries
                        (id, conversation_id, channel_id, message, props, status,
                         agent_run_id, root_id, message_kind, scope_id, artifact_refs,
                         file_ids, actions, update_post_id, idempotency_key,
                         actor_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            delivery_id,
                            message.conversation_id,
                            channel_id,
                            message.text,
                            _json(dict(message.props)),
                            agent_run_id,
                            message.root_id,
                            message.message_kind,
                            message.scope_id,
                            _json(list(message.artifact_refs)),
                            _json(list(message.file_ids)),
                            _json([dict(action) for action in message.actions]),
                            message.update_post_id,
                            stable_key,
                            message.actor_id or self._inbox_actor(event_id),
                            now,
                            now,
                        ),
                    )
                    stage = "lifecycle_insert"
                    self._connection.execute(
                        """INSERT INTO lifecycle_entities
                        (entity_type, entity_id, scope_id, state, payload, created_at, updated_at)
                        VALUES ('delivery', ?, ?, 'pending', '{}', ?, ?)""",
                        (delivery_id, message.conversation_id, now, now),
                    )
                    delivery_ids.append(delivery_id)
                stage = "inbox_update"
                self._connection.execute(
                    """UPDATE inbox_events SET status='completed', version=version+1,
                    last_error='', next_attempt_at=0, updated_at=? WHERE event_id=?""",
                    (now, event_id),
                )
                stage = "commit"
                self._connection.commit()
                log_event(
                    log,
                    "outbox.transaction.completed",
                    status="completed",
                    delivery_count=len(delivery_ids),
                )
            except Exception as error:
                self._connection.rollback()
                log_event(
                    log,
                    "outbox.transaction.failed",
                    level=40,
                    status="failed",
                    error_code=type(error).__name__,
                    transaction_stage=stage,
                )
                raise
        return tuple(delivery_ids)

    def claim_delivery(self) -> DeliveryRecord | None:
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM outbox_deliveries
                WHERE status IN ('pending', 'retrying') AND next_attempt_at <= ?
                ORDER BY created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                """UPDATE outbox_deliveries SET status='sending', attempts=attempts+1,
                updated_at=? WHERE id=?""",
                (now, row["id"]),
            )
            self._connection.commit()
        return self.get_delivery(str(row["id"]))

    def enqueue_delivery(self, message: OutboundMessage) -> str:
        """Persist one application-owned delivery without an Inbox parent."""
        stable_key = message.idempotency_key
        if not stable_key or not message.actor_id or not message.scope_id:
            raise ValueError("standalone delivery requires idempotency, actor, and scope")
        delivery_id = uuid.uuid5(uuid.NAMESPACE_URL, f"mmag:delivery:{stable_key}").hex
        channel_id = message.channel_id or message.conversation_id
        now = time.time()
        with self._lock:
            self._connection.execute(
                """INSERT OR IGNORE INTO outbox_deliveries
                (id, conversation_id, channel_id, message, props, status,
                 agent_run_id, root_id, message_kind, scope_id, artifact_refs,
                 file_ids, actions, update_post_id, idempotency_key,
                 actor_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    delivery_id,
                    message.conversation_id,
                    channel_id,
                    message.text,
                    _json(dict(message.props)),
                    message.agent_run_id,
                    message.root_id,
                    message.message_kind,
                    message.scope_id,
                    _json(list(message.artifact_refs)),
                    _json(list(message.file_ids)),
                    _json([dict(action) for action in message.actions]),
                    message.update_post_id,
                    stable_key,
                    message.actor_id,
                    now,
                    now,
                ),
            )
            self._connection.execute(
                """INSERT OR IGNORE INTO lifecycle_entities
                (entity_type, entity_id, scope_id, state, payload, created_at, updated_at)
                VALUES ('delivery', ?, ?, 'pending', '{}', ?, ?)""",
                (delivery_id, message.scope_id, now, now),
            )
            self._connection.commit()
        return delivery_id

    def retry_delivery(self, idempotency_key: str) -> str:
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE outbox_deliveries SET status='retrying', attempts=0,
                next_attempt_at=0, last_error='', updated_at=?
                WHERE idempotency_key=? AND status='failed'""",
                (time.time(), idempotency_key),
            )
            delivery_id = ""
            if cursor.rowcount:
                delivery_id = str(
                    self._connection.execute(
                        "SELECT id FROM outbox_deliveries WHERE idempotency_key=?",
                        (idempotency_key,),
                    ).fetchone()["id"]
                )
            self._connection.commit()
        return delivery_id

    def get_delivery(self, delivery_id: str) -> DeliveryRecord:
        row = self._connection.execute(
            "SELECT * FROM outbox_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return self._delivery_record(row)

    def list_deliveries(self, *, status: str | None = None) -> list[DeliveryRecord]:
        if status:
            rows = self._connection.execute(
                "SELECT * FROM outbox_deliveries WHERE status=? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM outbox_deliveries ORDER BY created_at"
            ).fetchall()
        return [self._delivery_record(row) for row in rows]

    def list_deliveries_for_run(self, agent_run_id: str) -> list[DeliveryRecord]:
        rows = self._connection.execute(
            "SELECT * FROM outbox_deliveries WHERE agent_run_id=? ORDER BY created_at",
            (agent_run_id,),
        ).fetchall()
        return [self._delivery_record(row) for row in rows]

    def recover_deliveries(self) -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE outbox_deliveries SET status='retrying', next_attempt_at=0,
                updated_at=? WHERE status='sending'""",
                (time.time(),),
            )
            self._connection.commit()

    def mark_delivery_delivered(self, delivery_id: str, remote_id: str) -> None:
        self._update_delivery(delivery_id, "delivered", remote_id=remote_id)

    def save_delivery_files(self, delivery_key: str, file_ids: tuple[str, ...]) -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE outbox_deliveries SET file_ids=?, updated_at=?
                WHERE id=? OR idempotency_key=?""",
                (_json(list(file_ids)), time.time(), delivery_key, delivery_key),
            )
            self._connection.commit()

    def create_action_token(
        self,
        *,
        jti: str,
        action: str,
        target: str,
        scope_id: str,
        run_id: str,
        expires_at: float,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO action_tokens
                (jti, action, target, scope_id, run_id, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (jti, action, target, scope_id, run_id, expires_at, time.time()),
            )
            self._connection.commit()

    def consume_action_token(self, jti: str, *, actor_id: str, now: float) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE action_tokens SET used_at=?, used_by=?
                WHERE jti=? AND used_at=0 AND expires_at>=?""",
                (now, actor_id, jti, now),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def mark_delivery_retry(self, delivery_id: str, error: str, retry_at: float) -> None:
        self._update_delivery(delivery_id, "retrying", error=error, retry_at=retry_at)

    def mark_delivery_failed(self, delivery_id: str, error: str) -> None:
        self._update_delivery(delivery_id, "failed", error=error)

    def create_lifecycle_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        state: str,
        scope_id: str,
        payload: dict[str, Any],
    ) -> LifecycleEntity:
        now = time.time()
        with self._lock:
            self._connection.execute(
                """INSERT INTO lifecycle_entities
                (entity_type, entity_id, scope_id, state, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entity_type.value, entity_id, scope_id, state, _json(payload), now, now),
            )
            self._connection.commit()
        return self.get_lifecycle_entity(entity_type, entity_id)

    def get_lifecycle_entity(self, entity_type: EntityType, entity_id: str) -> LifecycleEntity:
        row = self._connection.execute(
            "SELECT * FROM lifecycle_entities WHERE entity_type=? AND entity_id=?",
            (entity_type.value, entity_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"{entity_type.value}:{entity_id}")
        return self._lifecycle_entity(row)

    def find_transition(self, command_id: str) -> StateTransition | None:
        row = self._connection.execute(
            "SELECT * FROM state_transitions WHERE command_id=?", (command_id,)
        ).fetchone()
        return self._state_transition(row) if row else None

    def transition_lifecycle(
        self,
        current: LifecycleEntity,
        to_state: str,
        command_id: str,
        reason: str,
        actor_id: str,
        trace_id: str,
        payload_patch: dict[str, Any] | None = None,
    ) -> LifecycleEntity:
        now = time.time()
        payload = {**dict(current.payload), **(payload_patch or {})}
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                changed = self._connection.execute(
                    """UPDATE lifecycle_entities
                    SET state=?, version=version+1, payload=?, updated_at=?
                    WHERE entity_type=? AND entity_id=? AND version=?""",
                    (
                        to_state,
                        _json(payload),
                        now,
                        current.entity_type.value,
                        current.entity_id,
                        current.version,
                    ),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("version_conflict")
                self._connection.execute(
                    """INSERT INTO state_transitions
                    (command_id, entity_type, entity_id, from_state, to_state, version,
                     reason, actor_id, trace_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        command_id,
                        current.entity_type.value,
                        current.entity_id,
                        current.state,
                        to_state,
                        current.version + 1,
                        reason,
                        actor_id,
                        trace_id,
                        now,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return self.get_lifecycle_entity(current.entity_type, current.entity_id)

    def list_transitions(self, entity_type: EntityType, entity_id: str) -> list[StateTransition]:
        rows = self._connection.execute(
            """SELECT * FROM state_transitions WHERE entity_type=? AND entity_id=?
            ORDER BY version""",
            (entity_type.value, entity_id),
        ).fetchall()
        return [self._state_transition(row) for row in rows]

    def list_lifecycle_entities(self) -> list[LifecycleEntity]:
        rows = self._connection.execute("SELECT * FROM lifecycle_entities").fetchall()
        return [self._lifecycle_entity(row) for row in rows]

    def list_lifecycle_entities_for_scope(
        self,
        entity_type: EntityType,
        scope_id: str,
        *,
        limit: int = 50,
    ) -> list[LifecycleEntity]:
        if not scope_id or limit < 1 or limit > 100:
            raise ValueError("scope_id and a limit between 1 and 100 are required")
        rows = self._connection.execute(
            """SELECT * FROM lifecycle_entities
            WHERE entity_type=? AND scope_id=?
            ORDER BY updated_at DESC LIMIT ?""",
            (entity_type.value, scope_id, limit),
        ).fetchall()
        return [self._lifecycle_entity(row) for row in rows]

    def put_scope(self, scope: Scope) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO scopes
                (id, organization_id, project_id, customer_id, conversation_id,
                 platform, installation_id, tenant_id, kind, owner_id, team_id,
                 channel_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scope.id,
                    scope.organization_id,
                    scope.project_id,
                    scope.customer_id,
                    scope.conversation_id,
                    scope.platform,
                    scope.installation_id,
                    scope.tenant_id,
                    scope.kind.value,
                    scope.owner_id,
                    scope.team_id,
                    scope.channel_type,
                    time.time(),
                ),
            )
            self._connection.commit()

    def put_context(self, context: EnterpriseContext) -> None:
        now = time.time()
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO enterprise_entities
                (entity_type, entity_id, scope_id, name, content, metadata, source,
                 confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    context.entity_type,
                    context.entity_id,
                    context.scope_id,
                    context.name,
                    context.content,
                    _json(dict(context.metadata)),
                    context.source,
                    context.confidence,
                    now,
                    now,
                ),
            )
            self._connection.commit()

    def get_context(self, scope_id: str, *, entity_type: str = "") -> list[EnterpriseContext]:
        query = "SELECT * FROM enterprise_entities WHERE scope_id=?"
        params: tuple[str, ...] = (scope_id,)
        if entity_type:
            query += " AND entity_type=?"
            params = (scope_id, entity_type)
        rows = self._connection.execute(query + " ORDER BY updated_at DESC", params).fetchall()
        return [
            EnterpriseContext(
                row["entity_type"],
                row["entity_id"],
                row["scope_id"],
                row["name"],
                row["content"],
                row["source"],
                row["confidence"],
                json.loads(row["metadata"]),
            )
            for row in rows
        ]

    def append_audit(
        self,
        event_type: str,
        *,
        actor_id: str = "",
        scope_id: str = "",
        trace_id: str = "",
        target: str = "",
        decision: str = "",
        details: dict[str, Any] | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        with self._lock:
            self._connection.execute(
                """INSERT INTO audit_events
                (id, event_type, actor_id, scope_id, trace_id, target, decision, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    event_type,
                    actor_id,
                    scope_id,
                    trace_id,
                    target,
                    decision,
                    _json(details or {}),
                    time.time(),
                ),
            )
            self._connection.commit()
        return event_id

    def create_artifact(self, artifact: Artifact) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO artifacts
                (id, run_id, scope_id, kind, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.scope_id,
                    artifact.kind,
                    artifact.content,
                    _json(dict(artifact.metadata)),
                    time.time(),
                ),
            )
            self._connection.commit()

    def get_artifact(self, artifact_id: str) -> Artifact:
        row = self._connection.execute(
            "SELECT * FROM artifacts WHERE id=?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return Artifact(
            row["id"],
            row["run_id"],
            row["scope_id"],
            row["kind"],
            row["content"],
            json.loads(row["metadata"]),
        )

    def list_artifacts(self) -> list[Artifact]:
        rows = self._connection.execute("SELECT * FROM artifacts ORDER BY created_at").fetchall()
        return [
            Artifact(
                row["id"],
                row["run_id"],
                row["scope_id"],
                row["kind"],
                row["content"],
                json.loads(row["metadata"]),
            )
            for row in rows
        ]

    def list_audits(
        self,
        *,
        event_type: str = "",
        target: str = "",
        trace_id: str = "",
        actor_id: str = "",
        scope_id: str = "",
        decision: str = "",
        run_id: str = "",
        before: float | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        if limit < 1:
            raise ValueError("audit limit must be positive")
        filters: list[str] = []
        parameters: list[Any] = []
        if event_type:
            filters.append("event_type=?")
            parameters.append(event_type)
        if target:
            filters.append("target=?")
            parameters.append(target)
        for column, value in (
            ("trace_id", trace_id),
            ("actor_id", actor_id),
            ("scope_id", scope_id),
            ("decision", decision),
        ):
            if value:
                filters.append(f"{column}=?")
                parameters.append(value)
        if run_id:
            filters.append("json_extract(details, '$.run_id')=?")
            parameters.append(run_id)
        if before is not None:
            filters.append("created_at<?")
            parameters.append(before)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        rows = self._connection.execute(
            f"SELECT * FROM audit_events{where} ORDER BY created_at DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [
            AuditEvent(
                row["id"],
                row["event_type"],
                row["actor_id"],
                row["scope_id"],
                row["target"],
                row["decision"],
                row["trace_id"],
                json.loads(row["details"]),
                row["created_at"],
            )
            for row in rows
        ]

    def create_approval_request(self, request: ApprovalRequest) -> None:
        now = time.time()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """INSERT INTO lifecycle_entities
                    (entity_type, entity_id, scope_id, state, payload, created_at, updated_at)
                    VALUES ('approval_request', ?, ?, 'pending', ?, ?, ?)""",
                    (
                        request.id,
                        request.scope_id,
                        _json({"resume_token": request.resume_token}),
                        now,
                        now,
                    ),
                )
                self._connection.execute(
                    """INSERT INTO approval_requests
                    (id, capability_name, arguments, resume_token, requested_by, scope_id,
                     expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        request.id,
                        request.capability_name,
                        _json(dict(request.arguments)),
                        request.resume_token,
                        request.requested_by,
                        request.scope_id,
                        request.expires_at,
                        now,
                        now,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_approval_request(self, request_id: str) -> ApprovalRequest:
        row = self._connection.execute(
            """SELECT approval.*, lifecycle.state FROM approval_requests approval
            JOIN lifecycle_entities lifecycle
              ON lifecycle.entity_type='approval_request' AND lifecycle.entity_id=approval.id
            WHERE approval.id=?""",
            (request_id,),
        ).fetchone()
        if row is None:
            raise KeyError(request_id)
        from .models import ApprovalRequestState

        return ApprovalRequest(
            row["id"],
            row["capability_name"],
            json.loads(row["arguments"]),
            row["resume_token"],
            row["requested_by"],
            row["scope_id"],
            row["expires_at"],
            ApprovalRequestState(row["state"]),
        )

    def record_approval_decision(self, request_id: str, actor_id: str, reason: str) -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE approval_requests SET decided_by=?, decision_reason=?, updated_at=?
                WHERE id=?""",
                (actor_id, reason, time.time(), request_id),
            )
            self._connection.commit()

    def _update_inbox(self, event_id: str, status: str, error: str = "") -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE inbox_events SET status=?, version=version+1, last_error=?,
                updated_at=? WHERE event_id=?""",
                (status, error, time.time(), event_id),
            )
            self._connection.commit()

    def _update_delivery(
        self,
        delivery_id: str,
        status: str,
        *,
        error: str = "",
        retry_at: float = 0,
        remote_id: str = "",
    ) -> None:
        with self._lock:
            self._connection.execute(
                """UPDATE outbox_deliveries SET status=?, last_error=?, next_attempt_at=?,
                remote_id=?, updated_at=? WHERE id=?""",
                (status, error, retry_at, remote_id, time.time(), delivery_id),
            )
            self._connection.commit()

    @staticmethod
    def _inbox_record(row: Any) -> InboxRecord:
        return InboxRecord(
            InboundEvent(
                row["event_id"],
                row["platform"],
                row["event_type"],
                row["conversation_id"],
                row["actor_id"],
                row["occurred_at"],
                json.loads(row["payload"]),
            ),
            row["status"],
            row["version"],
            row["last_error"],
            row["attempts"],
            row["next_attempt_at"],
        )

    @staticmethod
    def _delivery_record(row: Any) -> DeliveryRecord:
        return DeliveryRecord(
            row["id"],
            OutboundMessage(
                row["conversation_id"],
                row["message"],
                row["channel_id"],
                json.loads(row["props"]),
                row["agent_run_id"],
                row["idempotency_key"] or row["id"],
                row["root_id"],
                row["message_kind"],
                row["scope_id"],
                tuple(json.loads(row["artifact_refs"])),
                tuple(json.loads(row["file_ids"])),
                tuple(json.loads(row["actions"])),
                row["update_post_id"],
                row["actor_id"],
            ),
            row["status"],
            row["attempts"],
            row["next_attempt_at"],
            row["last_error"],
            row["remote_id"],
        )

    def _inbox_actor(self, event_id: str) -> str:
        row = self._connection.execute(
            "SELECT actor_id FROM inbox_events WHERE event_id=?", (event_id,)
        ).fetchone()
        return str(row["actor_id"]) if row is not None else ""

    @staticmethod
    def _lifecycle_entity(row: Any) -> LifecycleEntity:
        return LifecycleEntity(
            EntityType(row["entity_type"]),
            row["entity_id"],
            row["state"],
            row["version"],
            row["scope_id"],
            json.loads(row["payload"]),
        )

    @staticmethod
    def _state_transition(row: Any) -> StateTransition:
        return StateTransition(
            row["command_id"],
            EntityType(row["entity_type"]),
            row["entity_id"],
            row["from_state"],
            row["to_state"],
            row["version"],
            row["reason"],
            row["actor_id"],
            row["trace_id"],
        )
