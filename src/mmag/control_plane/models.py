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
    actor_id: str = ""


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


class PersonalSkillStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkCaseStatus(StrEnum):
    CANDIDATE = "candidate"
    SAVED = "saved"
    ARCHIVED = "archived"


class InteractionStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MemoryItemKind(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    RELATIONSHIP = "relationship"
    COMMITMENT = "commitment"


class MemoryItemStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class DigitalPersonaStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class PersonaReplyState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXPIRED = "expired"


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
    WAITING_CHILD = "waiting_child"
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
class AgentRunSpec:
    """Immutable trusted identity used to create one durable AgentRun."""

    run_id: str
    workflow_id: str
    actor_id: str
    scope_id: str
    trace_id: str
    thread_id: str
    agent_ref: str
    package_snapshot: Mapping[str, Any]
    parent_run_id: str = ""
    parent_tool_call_id: str = ""
    skill_ref: str = ""

    def __post_init__(self) -> None:
        required = (
            self.run_id,
            self.workflow_id,
            self.actor_id,
            self.scope_id,
            self.trace_id,
            self.thread_id,
            self.agent_ref,
        )
        if not all(value.strip() for value in required) or not self.package_snapshot:
            raise ValueError("AgentRun identity and package snapshot are required")
        if bool(self.parent_run_id) != bool(self.parent_tool_call_id):
            raise ValueError("child AgentRun requires parent run and parent tool call together")


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    """Durable AgentRun identity and lifecycle projection."""

    run_id: str
    workflow_id: str
    actor_id: str
    scope_id: str
    trace_id: str
    thread_id: str
    agent_ref: str
    package_snapshot: Mapping[str, Any]
    state: AgentRunState
    version: int
    execution_key: str = ""
    parent_run_id: str = ""
    parent_tool_call_id: str = ""
    skill_ref: str = ""


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
class PersonalSkill:
    id: str
    revision: int
    installation_id: str
    tenant_id: str
    owner_id: str
    scope_id: str
    name: str
    description: str
    base_skill_ref: str
    preferred_agent: str
    activation_intents: tuple[str, ...]
    activation_keywords: tuple[str, ...]
    auto_select: bool
    instruction: str
    template: str
    sha256: str
    source_case_ids: tuple[str, ...] = ()
    status: PersonalSkillStatus = PersonalSkillStatus.DRAFT

    @property
    def ref(self) -> str:
        return f"pskill://{self.id}@{self.revision}"


@dataclass(frozen=True, slots=True)
class WorkCase:
    id: str
    installation_id: str
    tenant_id: str
    owner_id: str
    scope_id: str
    goal: str
    result_summary: str
    agent_name: str = ""
    skill_ref: str = ""
    personal_skill_ref: str = ""
    source_run_id: str = ""
    source_message_id: str = ""
    goal_hash: str = ""
    result_hash: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    feedback: str = ""
    status: WorkCaseStatus = WorkCaseStatus.CANDIDATE
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class InteractionSession:
    id: str
    installation_id: str
    tenant_id: str
    owner_id: str
    scope_id: str
    conversation_id: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    expires_at: float = 0.0
    status: InteractionStatus = InteractionStatus.OPEN


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    installation_id: str
    tenant_id: str
    owner_id: str
    scope_id: str
    kind: MemoryItemKind
    content: str
    content_hash: str
    source_refs: tuple[str, ...] = ()
    confidence: float = 1.0
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE
    expires_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def ref(self) -> str:
        return f"memory://{self.id}"


@dataclass(frozen=True, slots=True)
class DigitalPersona:
    id: str
    revision: int
    installation_id: str
    tenant_id: str
    owner_id: str
    owner_username: str
    scope_id: str
    display_name: str
    allowed_topics: tuple[str, ...]
    approval_topics: tuple[str, ...]
    denied_topics: tuple[str, ...]
    response_mode: str
    source_memory_ids: tuple[str, ...]
    published_snapshots: tuple[Mapping[str, Any], ...]
    sha256: str
    status: DigitalPersonaStatus = DigitalPersonaStatus.DRAFT
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def ref(self) -> str:
        return f"persona://{self.id}@{self.revision}"


@dataclass(frozen=True, slots=True)
class PersonaReplyRequest:
    id: str
    installation_id: str
    tenant_id: str
    persona_ref: str
    persona_hash: str
    owner_id: str
    requester_id: str
    requester_username: str
    source_scope_id: str
    source_channel_id: str
    source_root_id: str
    source_status_post_id: str
    owner_approval_post_id: str
    owner_channel_id: str
    question: str
    draft_text: str
    approval_reason: str
    expires_at: float
    state: PersonaReplyState = PersonaReplyState.PENDING
    decision_by: str = ""
    decided_at: float = 0.0
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


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
