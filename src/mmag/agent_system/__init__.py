"""Canonical Managed Agent domain and orchestration surface."""

from .capability_agent import CapabilityAgent
from .core import (
    AgentDescriptor,
    AgentOutput,
    AgentRegistry,
    AgentRequest,
    AgentRouter,
    AgentSelection,
    HandoffCoordinator,
    HandoffResult,
    HandoffStep,
    ManagedAgent,
    RuntimeAgent,
)

__all__ = [
    "AgentDescriptor",
    "AgentOutput",
    "AgentRegistry",
    "AgentRequest",
    "AgentRouter",
    "AgentSelection",
    "CapabilityAgent",
    "HandoffCoordinator",
    "HandoffResult",
    "HandoffStep",
    "ManagedAgent",
    "RuntimeAgent",
]
