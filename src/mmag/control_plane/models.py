"""Immutable contracts for the durable application control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: str
    platform: str
    event_type: str
    conversation_id: str
    actor_id: str
    occurred_at: float
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.event_id or not self.conversation_id:
            raise ValueError("event_id and conversation_id are required")


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    conversation_id: str
    text: str
    channel_id: str = ""
    props: Mapping[str, Any] = field(default_factory=dict)
    agent_run_id: str = ""
    idempotency_key: str = ""
    root_id: str = ""
    message_kind: str = "result"
    scope_id: str = ""
    artifact_refs: tuple[str, ...] = ()
    file_ids: tuple[str, ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()
    update_post_id: str = ""


@dataclass(frozen=True, slots=True)
class InboxRecord:
    event: InboundEvent
    status: str
    version: int
    last_error: str = ""
    attempts: int = 0
    next_attempt_at: float = 0.0


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    id: str
    message: OutboundMessage
    status: str
    attempts: int
    next_attempt_at: float
    last_error: str = ""
    remote_id: str = ""


class EntityType(StrEnum):
    TASK = "task"
    AGENT_RUN = "agent_run"
    CAPABILITY_CALL = "capability_call"
    APPROVAL_REQUEST = "approval_request"
    DELIVERY = "delivery"


class ScopeKind(StrEnum):
    PERSONAL = "personal"
    CHANNEL = "channel"
    PROJECT = "project"
    TASK = "task"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CapabilityCallState(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalRequestState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class DeliveryState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRYING = "retrying"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class LifecycleEntity:
    entity_type: EntityType
    entity_id: str
    state: str
    version: int
    scope_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StateTransition:
    command_id: str
    entity_type: EntityType
    entity_id: str
    from_state: str
    to_state: str
    version: int
    reason: str = ""
    actor_id: str = ""
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class Scope:
    id: str
    organization_id: str = ""
    project_id: str = ""
    customer_id: str = ""
    conversation_id: str = ""
    platform: str = ""
    installation_id: str = ""
    tenant_id: str = ""
    kind: ScopeKind = ScopeKind.CHANNEL
    owner_id: str = ""
    team_id: str = ""
    channel_type: str = ""


@dataclass(frozen=True, slots=True)
class Principal:
    platform: str
    installation_id: str
    tenant_id: str
    actor_id: str

    def __post_init__(self) -> None:
        if not all((self.platform, self.installation_id, self.tenant_id, self.actor_id)):
            raise ValueError("principal identity is incomplete")


@dataclass(frozen=True, slots=True)
class EnterpriseContext:
    entity_type: str
    entity_id: str
    scope_id: str
    name: str = ""
    content: str = ""
    source: str = ""
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    scope_id: str
    title: str
    state: TaskState = TaskState.QUEUED


@dataclass(frozen=True, slots=True)
class TaskStep:
    id: str
    task_id: str
    agent_name: str
    sequence: int
    state: str = "queued"


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    run_id: str
    scope_id: str
    kind: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    capability_name: str
    arguments: Mapping[str, Any]
    resume_token: str
    requested_by: str
    scope_id: str = ""
    expires_at: float | None = None
    state: ApprovalRequestState = ApprovalRequestState.PENDING


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    event_type: str
    actor_id: str
    scope_id: str
    target: str
    decision: str
    trace_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
