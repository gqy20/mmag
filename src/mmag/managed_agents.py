"""Registry, deterministic routing and handoff for managed digital workers."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    description: str
    intents: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("*",)
    max_cost_usd: float = 1.0
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class AgentRouteRequest:
    intent: str
    prompt: str
    scope: str = "*"
    permissions: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset()
    budget_usd: float = 1.0
    artifacts: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class ManagedAgentResult:
    text: str
    agent_name: str
    artifacts: tuple[dict, ...] = ()


class ManagedAgent(Protocol):
    spec: AgentSpec

    async def run(self, request: AgentRouteRequest) -> ManagedAgentResult: ...


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, ManagedAgent] = {}

    def register(self, agent: ManagedAgent) -> None:
        if agent.spec.name in self._agents:
            raise ValueError(f"agent {agent.spec.name!r} is already registered")
        self._agents[agent.spec.name] = agent

    def get(self, name: str) -> ManagedAgent:
        try:
            return self._agents[name]
        except KeyError as error:
            raise LookupError(f"unknown managed agent {name!r}") from error

    def list(self) -> tuple[ManagedAgent, ...]:
        return tuple(self._agents.values())


class AgentRouter:
    """Filter hard constraints first, then rank declared intent fit."""

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def route(self, request: AgentRouteRequest) -> ManagedAgent:
        candidates = [
            agent for agent in self.registry.list() if self._eligible(agent.spec, request)
        ]
        if not candidates:
            raise LookupError(f"no managed agent can serve intent {request.intent!r}")
        return max(candidates, key=lambda agent: self._score(agent.spec, request))

    @staticmethod
    def _eligible(spec: AgentSpec, request: AgentRouteRequest) -> bool:
        return (
            spec.healthy
            and set(spec.permissions).issubset(request.permissions)
            and request.required_capabilities.issubset(spec.capabilities)
            and spec.max_cost_usd <= request.budget_usd
            and any(fnmatch(request.scope, pattern) for pattern in spec.scopes)
        )

    @staticmethod
    def _score(spec: AgentSpec, request: AgentRouteRequest) -> tuple[int, int, str]:
        exact = int(request.intent.lower() in {intent.lower() for intent in spec.intents})
        partial = sum(intent.lower() in request.prompt.lower() for intent in spec.intents)
        return exact, partial, spec.name


@dataclass(frozen=True, slots=True)
class HandoffStep:
    agent_name: str
    status: str
    text: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class HandoffResult:
    text: str
    steps: tuple[HandoffStep, ...]
    artifacts: tuple[dict, ...] = field(default_factory=tuple)


class HandoffOrchestrator:
    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    async def run(self, request: AgentRouteRequest, agent_names: tuple[str, ...]) -> HandoffResult:
        steps: list[HandoffStep] = []
        artifacts = list(request.artifacts)
        last_text = ""
        for name in agent_names:
            try:
                agent = self.registry.get(name)
                result = await agent.run(
                    AgentRouteRequest(
                        request.intent,
                        last_text or request.prompt,
                        request.scope,
                        request.permissions,
                        request.required_capabilities,
                        request.budget_usd,
                        tuple(artifacts),
                    )
                )
                last_text = result.text
                artifacts.extend(result.artifacts)
                steps.append(HandoffStep(name, "completed", result.text))
            except Exception as error:
                steps.append(HandoffStep(name, "failed", error=str(error)))
        return HandoffResult(last_text, tuple(steps), tuple(artifacts))


class RuntimeManagedAgent:
    """Adapt a provider-neutral runtime into a registered managed agent."""

    def __init__(self, spec: AgentSpec, runtime, request_factory=None):
        self.spec = spec
        self._runtime = runtime
        self._request_factory = request_factory

    async def run(self, request: AgentRouteRequest) -> ManagedAgentResult:
        runtime_request = (
            self._request_factory(request, self.spec)
            if self._request_factory is not None
            else self._default_request(request)
        )
        result = await self._runtime.run(runtime_request)
        return ManagedAgentResult(
            result.text,
            self.spec.name,
            tuple(dict(artifact) for artifact in result.artifacts),
        )

    def _default_request(self, request: AgentRouteRequest):
        from .runtimes import RunContext, RunRequest

        return RunRequest(
            context=RunContext(
                trace_id=uuid.uuid4().hex[:12],
                actor_id="managed-agent",
                conversation_id=request.scope,
                scope=request.scope,
            ),
            messages=({"role": "user", "content": request.prompt},),
            system_prompt=self.spec.description,
        )


class LinkAgent:
    """First vertical managed agent backed by the canonical analyze-link capability."""

    spec = AgentSpec(
        name="link",
        description="Analyze a URL and return a sourced artifact",
        intents=("link", "url", "链接"),
        capabilities=("analyze_link",),
        permissions=("web:read",),
        scopes=("*",),
        max_cost_usd=0.1,
    )

    def __init__(self, capability_spec, executor):
        self._capability_spec = capability_spec
        self._executor = executor

    async def run(self, request: AgentRouteRequest) -> ManagedAgentResult:
        match = re.search(r"https?://[^\s<>]+", request.prompt)
        if not match:
            raise ValueError("LinkAgent requires an http(s) URL")
        url = match.group(0).rstrip(".,;!?)")
        result = await self._executor.execute(self._capability_spec, {"url": url})
        payload = result.to_payload()
        artifact = {
            "kind": "link_analysis",
            "source": url,
            "content": payload,
        }
        return ManagedAgentResult(
            json.dumps(payload, ensure_ascii=False, default=str),
            self.spec.name,
            (artifact,),
        )
