"""Managed Agent contracts, routing, handoff, and built-in implementations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    name: str
    description: str
    intents: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("*",)
    max_cost_usd: float = 1.0
    healthy: bool = True
    is_default: bool = False
    routing_priority: int = 0
    routing_keywords: tuple[str, ...] = ()
    requires_url: bool = False


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    """One resolved Skill carried through the trusted runtime boundary."""

    ref: str
    capabilities: tuple[str, ...]
    provenance: Mapping[str, str]
    personal_ref: str = ""
    personal_instruction: str = ""
    personal_template: str = ""


@dataclass(frozen=True, slots=True)
class AgentRequest:
    intent: str
    prompt: str
    scope: str = "*"
    permissions: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset()
    budget_usd: float = 1.0
    artifacts: tuple[dict, ...] = ()
    actor_id: str = "managed-agent"
    task_id: str = ""
    run_id: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    context_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    runtime_request: Any | None = None
    requested_skill: str = ""
    requested_personal_skill: str = ""
    requested_agent: str = ""
    skill: SkillInvocation | None = None
    preferred_agents: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    response_style: str = ""
    language: str = ""


@dataclass(frozen=True, slots=True)
class AgentOutput:
    text: str
    agent_name: str
    artifacts: tuple[dict, ...] = ()
    result: dict[str, Any] | None = None
    envelope: dict[str, Any] | None = None
    runtime_result: Any | None = None


@dataclass(frozen=True, slots=True)
class AgentSelection:
    agent: ManagedAgent
    intent: str
    reason: str = ""
    matched_keywords: tuple[str, ...] = ()
    candidate_count: int = 0


class ManagedAgent(Protocol):
    descriptor: AgentDescriptor

    async def run(self, request: AgentRequest) -> AgentOutput: ...


class AgentRegistry:
    def __init__(self, agents: tuple[ManagedAgent, ...] = ()) -> None:
        self._agents: dict[str, ManagedAgent] = {}
        for agent in agents:
            self.register(agent)

    def register(self, agent: ManagedAgent) -> None:
        if agent.descriptor.name in self._agents:
            raise ValueError(f"agent {agent.descriptor.name!r} is already registered")
        if agent.descriptor.is_default and any(
            current.descriptor.is_default for current in self._agents.values()
        ):
            raise ValueError("only one default managed agent may be registered")
        if any(
            self._routing_signature(current.descriptor) == self._routing_signature(agent.descriptor)
            for current in self._agents.values()
        ):
            raise ValueError(f"ambiguous routing declaration for agent {agent.descriptor.name!r}")
        self._agents[agent.descriptor.name] = agent

    @staticmethod
    def _routing_signature(descriptor: AgentDescriptor) -> tuple:
        return (
            descriptor.is_default,
            descriptor.routing_priority,
            frozenset(intent.lower() for intent in descriptor.intents),
            frozenset(keyword.lower() for keyword in descriptor.routing_keywords),
            descriptor.requires_url,
            frozenset(descriptor.scopes),
        )

    def get(self, name: str) -> ManagedAgent:
        try:
            return self._agents[name]
        except KeyError as error:
            raise LookupError(f"unknown managed agent {name!r}") from error

    def list(self) -> tuple[ManagedAgent, ...]:
        return tuple(self._agents.values())

    def default(self) -> ManagedAgent:
        defaults = tuple(agent for agent in self._agents.values() if agent.descriptor.is_default)
        if len(defaults) != 1:
            raise LookupError("exactly one default managed agent is required")
        return defaults[0]


class AgentRouter:
    """Filter hard constraints first, then rank declared intent fit."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def route(self, request: AgentRequest) -> AgentSelection:
        candidates = [
            agent for agent in self.registry.list() if self._eligible(agent.descriptor, request)
        ]
        if not candidates:
            raise LookupError(f"no managed agent can serve intent {request.intent!r}")
        agent = max(candidates, key=lambda item: self._score(item.descriptor, request))
        descriptor = agent.descriptor
        intent = (
            request.intent
            if request.intent.lower() in {item.lower() for item in descriptor.intents}
            else descriptor.intents[0]
        )
        matched_keywords = tuple(
            keyword
            for keyword in descriptor.routing_keywords
            if keyword.lower() in request.prompt.lower()
        )
        return AgentSelection(
            agent,
            intent,
            reason=self._reason(descriptor, request, matched_keywords),
            matched_keywords=matched_keywords,
            candidate_count=len(candidates),
        )

    def default(self, request: AgentRequest) -> AgentSelection:
        agent = self.registry.default()
        if not self._hard_constraints(agent.descriptor, request):
            raise LookupError("default managed agent cannot serve the request")
        intent = (
            request.intent
            if request.intent in agent.descriptor.intents
            else agent.descriptor.intents[0]
        )
        return AgentSelection(agent, intent, reason="default", candidate_count=1)

    @staticmethod
    def _reason(
        descriptor: AgentDescriptor,
        request: AgentRequest,
        matched_keywords: tuple[str, ...],
    ) -> str:
        if request.requested_agent == descriptor.name:
            return "requested_agent"
        if matched_keywords:
            return "keyword"
        if request.intent.lower() in {intent.lower() for intent in descriptor.intents}:
            return "intent"
        if descriptor.name.lower() in request.preferred_agents:
            return "preferred_agent"
        return "default"

    @staticmethod
    def _eligible(descriptor: AgentDescriptor, request: AgentRequest) -> bool:
        return AgentRouter._hard_constraints(descriptor, request) and AgentRouter._matches_route(
            descriptor, request
        )

    @staticmethod
    def _hard_constraints(descriptor: AgentDescriptor, request: AgentRequest) -> bool:
        return (
            descriptor.healthy
            and set(descriptor.permissions).issubset(request.permissions)
            and request.required_capabilities.issubset(descriptor.capabilities)
            and descriptor.max_cost_usd <= request.budget_usd
            and any(fnmatch(request.scope, pattern) for pattern in descriptor.scopes)
        )

    @staticmethod
    def _matches_route(descriptor: AgentDescriptor, request: AgentRequest) -> bool:
        if request.requested_agent:
            return descriptor.name == request.requested_agent
        prompt = request.prompt.lower()
        exact_intent = request.intent.lower() in {intent.lower() for intent in descriptor.intents}
        keyword_match = any(keyword.lower() in prompt for keyword in descriptor.routing_keywords)
        has_url = bool(re.search(r"https?://[^\s<>]+", request.prompt, re.I))
        if descriptor.requires_url and not has_url:
            return False
        return descriptor.is_default or exact_intent or keyword_match

    @staticmethod
    def _score(
        descriptor: AgentDescriptor, request: AgentRequest
    ) -> tuple[int, int, int, int, int, str]:
        exact = int(request.intent.lower() in {intent.lower() for intent in descriptor.intents})
        keywords = sum(
            keyword.lower() in request.prompt.lower() for keyword in descriptor.routing_keywords
        )
        specialized = int(not descriptor.is_default and bool(exact or keywords))
        preferred = int(descriptor.name.lower() in request.preferred_agents)
        requested = int(descriptor.name == request.requested_agent)
        return (
            specialized,
            max(preferred, requested),
            descriptor.routing_priority,
            exact,
            keywords,
            descriptor.name,
        )


class RuntimeAgent:
    """Adapt a provider-neutral runtime into a registered managed agent."""

    def __init__(
        self,
        descriptor: AgentDescriptor,
        runtime,
        request_factory,
    ):
        self.descriptor = descriptor
        self._runtime = runtime
        self._request_factory = request_factory

    async def run(self, request: AgentRequest) -> AgentOutput:
        runtime_request = self._request_factory(request, self.descriptor)
        result = await self._runtime.run(runtime_request)
        return AgentOutput(
            text=result.text,
            agent_name=self.descriptor.name,
            artifacts=tuple(dict(artifact) for artifact in result.artifacts),
            result=dict(result.output) if result.output is not None else None,
            runtime_result=result,
        )
