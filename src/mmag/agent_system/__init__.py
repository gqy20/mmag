"""Canonical Managed Agent domain and orchestration surface."""

from .capability_agent import CapabilityAgent
from .core import (
    AgentDescriptor,
    AgentOutput,
    AgentRegistry,
    AgentRequest,
    AgentRouter,
    AgentSelection,
    ManagedAgent,
    RuntimeAgent,
    SkillInvocation,
)
from .dispatcher import AgentDispatchResult, AgentDispatchTarget, RunCoordinator

__all__ = [
    "AgentDescriptor",
    "AgentDispatchResult",
    "AgentDispatchTarget",
    "AgentOutput",
    "AgentRegistry",
    "AgentRequest",
    "AgentRouter",
    "AgentSelection",
    "CapabilityAgent",
    "ManagedAgent",
    "RuntimeAgent",
    "RunCoordinator",
    "SkillInvocation",
]
