"""Immutable request context available while a capability is executing."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class CapabilityContext:
    """Identity and originating message for one capability execution."""

    trace_id: str
    actor_id: str
    conversation_id: str
    message_id: str
    message: str
    scope: str = ""
    allowed_capabilities: frozenset[str] = frozenset()
    run_id: str = ""
    allowed_execution_profiles: frozenset[str] = frozenset()


_CURRENT_CONTEXT: ContextVar[CapabilityContext | None] = ContextVar(
    "mmag_capability_context",
    default=None,
)


def get_capability_context() -> CapabilityContext | None:
    """Return the context bound to the current async request, if any."""
    return _CURRENT_CONTEXT.get()


@contextmanager
def bind_capability_context(context: CapabilityContext) -> Iterator[None]:
    """Bind an immutable context and reliably restore the previous value."""
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)
