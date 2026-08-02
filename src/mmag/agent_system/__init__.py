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

__all__ = [
    "AgentDescriptor",
    "AgentOutput",
    "AgentRegistry",
    "AgentRequest",
    "AgentRouter",
    "AgentSelection",
    "CapabilityAgent",
    "ManagedAgent",
    "RuntimeAgent",
    "SkillInvocation",
]
