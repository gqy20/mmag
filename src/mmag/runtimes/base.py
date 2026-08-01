"""Provider-neutral Agent Runtime contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return mutable JSON-like values from immutable Runtime contracts."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def remaining_seconds(request: RunRequest) -> float | None:
    """Calculate the remaining Runtime deadline using matching timezone semantics."""
    deadline = request.context.deadline
    if deadline is None:
        return None
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
    return (deadline - now).total_seconds()


@dataclass(frozen=True, slots=True)
class RunContext:
    """Stable identity and scheduling context for one Agent run."""

    trace_id: str
    actor_id: str
    conversation_id: str
    scope: str
    deadline: datetime | None = None
    run_id: str = ""


class RunEventKind(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    APPROVAL_REQUIRED = "approval_required"
    ARTIFACT_CREATED = "artifact_created"
    RUN_STATUS = "run_status"


@dataclass(frozen=True, slots=True)
class RunEvent:
    kind: RunEventKind
    text: str = ""
    round: int = 0
    name: str = ""
    data: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@runtime_checkable
class RunEventSink(Protocol):
    async def __call__(self, event: RunEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Provider-neutral input for one Agent run.

    Multimodal attachments remain represented as content blocks inside messages
    until the Capability/Artifact contract is introduced.
    """

    context: RunContext
    messages: tuple[Mapping[str, Any], ...]
    system_prompt: str = ""
    capabilities: tuple[Mapping[str, Any], ...] = ()
    max_rounds: int = 5
    max_tokens: int = 4096
    fallback_max_tokens: int = 1024
    temperature: float = 0.0
    response_schema: Mapping[str, Any] | None = None
    skill_files: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    event_sink: RunEventSink | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        if self.fallback_max_tokens < 1:
            raise ValueError("fallback_max_tokens must be at least 1")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        object.__setattr__(self, "messages", tuple(_freeze(message) for message in self.messages))
        object.__setattr__(
            self,
            "capabilities",
            tuple(_freeze(capability) for capability in self.capabilities),
        )
        if self.response_schema is not None:
            object.__setattr__(self, "response_schema", _freeze(self.response_schema))
        object.__setattr__(self, "skill_files", _freeze(self.skill_files))


class RuntimeStatus(StrEnum):
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    WAITING_APPROVAL = "waiting_approval"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class AgentResult:
    text: str
    runtime: str
    status: RuntimeStatus = RuntimeStatus.COMPLETED
    artifacts: tuple[Mapping[str, Any], ...] = ()
    deliveries: tuple[Mapping[str, Any], ...] = ()
    capability_calls: tuple[Mapping[str, Any], ...] = ()
    interruptions: tuple[Mapping[str, Any], ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    output: Mapping[str, Any] | None = None


@runtime_checkable
class AgentRuntime(Protocol):
    async def run(self, request: RunRequest) -> AgentResult: ...


class AgentRuntimeError(Exception):
    """Base provider-neutral failure exposed to application code."""

    code = "runtime_failure"
    retryable = False

    def __init__(self, message: str, *, runtime: str):
        super().__init__(message)
        self.runtime = runtime


class RuntimeTimeoutError(AgentRuntimeError):
    code = "timeout"
    retryable = True


class RuntimeRateLimitError(AgentRuntimeError):
    code = "rate_limited"
    retryable = True


class RuntimeRejectedError(AgentRuntimeError):
    code = "rejected"


class RuntimeUnavailableError(AgentRuntimeError):
    code = "unavailable"
    retryable = True


class RuntimeInternalError(AgentRuntimeError):
    code = "internal"


def _error_chain(error: Exception) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def translate_runtime_error(error: Exception, *, runtime: str) -> AgentRuntimeError:
    """Translate backend-specific failures into stable application semantics."""
    chain = _error_chain(error)
    message = str(error) or type(error).__name__
    searchable = " ".join(f"{type(item).__name__} {item}".lower() for item in chain)

    if any(isinstance(item, TimeoutError) for item in chain) or "timeout" in searchable:
        return RuntimeTimeoutError(message, runtime=runtime)
    if "rate limit" in searchable or "ratelimit" in searchable or "status 429" in searchable:
        return RuntimeRateLimitError(message, runtime=runtime)
    if any(token in searchable for token in ("content policy", "rejected", "permission denied")):
        return RuntimeRejectedError(message, runtime=runtime)
    if any(isinstance(item, ConnectionError) for item in chain) or any(
        token in searchable
        for token in ("connection", "disconnect", "unavailable", "status 502", "status 503")
    ):
        return RuntimeUnavailableError(message, runtime=runtime)
    return RuntimeInternalError(message, runtime=runtime)
