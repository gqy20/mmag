"""Trusted execution providers and atomic Agent construction from YAML Packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from ..agent_system import AgentDescriptor, CapabilityAgent, ManagedAgent, RuntimeAgent
from ..capabilities import CapabilityEffect
from ..runtimes import RunContext, RunRequest
from .assets import render_prompt
from .errors import AgentPackageError
from .runtime import (
    ContractAgentDecorator,
    PackageAgentRunner,
    build_agent_descriptor,
    build_task_message,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from ..agent_system import AgentRequest
    from ..capabilities import CapabilityExecutor, CapabilityRegistry
    from ..governance import ModelGateway, ModelPolicyRegistry
    from ..skill_packages import SkillResourceLoader
    from .models import AgentPackage


class AgentProvider(Protocol):
    kind: str
    name: str

    def create(self, package: AgentPackage) -> ManagedAgent: ...


class AgentProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[str, str], AgentProvider] = {}

    def register(self, provider: AgentProvider) -> None:
        key = (provider.kind, provider.name)
        if key in self._providers:
            raise ValueError(f"Agent provider {key!r} is already registered")
        self._providers[key] = provider

    def get(self, kind: str, name: str) -> AgentProvider:
        try:
            return self._providers[(kind, name)]
        except KeyError as error:
            raise AgentPackageError(f"unknown Agent execution provider {kind}/{name}") from error


class AgentFactory:
    def __init__(self, providers: AgentProviderRegistry) -> None:
        self.providers = providers

    def create(self, package: AgentPackage) -> ManagedAgent:
        execution = package.manifest.execution
        if package.manifest.routing.default and execution.kind != "langgraph":
            raise AgentPackageError("the default Agent must use a LangGraph provider")
        return self.providers.get(execution.kind, execution.provider).create(package)

    def create_all(self, packages: tuple[AgentPackage, ...]) -> tuple[ManagedAgent, ...]:
        return tuple(self.create(package) for package in packages)


@dataclass(slots=True)
class RoutedModelRuntime:
    gateway: ModelGateway
    route: str

    async def run(self, request: RunRequest):
        return await self.gateway.run(request, route=self.route)


class LangGraphTextProvider:
    kind = "langgraph"
    name = "text-v1"

    def __init__(
        self,
        gateway: ModelGateway,
        capabilities: CapabilityRegistry,
        model_policies: ModelPolicyRegistry,
        *,
        additional_capabilities: tuple[str, ...] = (),
        skill_resources: SkillResourceLoader | None = None,
    ) -> None:
        self.gateway = gateway
        self.capabilities = capabilities
        self.model_policies = model_policies
        self.additional_capabilities = additional_capabilities
        self.skill_resources = skill_resources

    def create(self, package: AgentPackage) -> ManagedAgent:
        names = self._capability_names(package)
        descriptor = build_agent_descriptor(
            package,
            AgentDescriptor(
                package.manifest.metadata.name,
                package.manifest.metadata.description,
                capabilities=names,
                scopes=package.manifest.routing.scopes,
            ),
        )
        runtime = RoutedModelRuntime(self.gateway, package.manifest.runtime.route)
        delegate = RuntimeAgent(
            descriptor,
            runtime,
            request_factory=self._request_factory(package, names),
            use_prepared_request=package.manifest.routing.default,
        )
        return ContractAgentDecorator(package, delegate, self.skill_resources)

    def _capability_names(self, package: AgentPackage) -> tuple[str, ...]:
        declaration = package.manifest.capabilities
        return self.capabilities.resolve_names(
            declaration.allow,
            declaration.deny,
            additional_names=self.additional_capabilities,
        )

    def _request_factory(self, package: AgentPackage, names: tuple[str, ...]):
        model_policy = self.model_policies.get(package.manifest.model_policy_ref)

        def build(request: AgentRequest, descriptor: AgentDescriptor) -> RunRequest:
            del descriptor
            now = datetime.now(UTC)
            prepared = request.runtime_request
            conversation_id = (
                prepared.context.conversation_id
                if isinstance(prepared, RunRequest)
                else request.scope.rsplit("/", 1)[-1]
            )
            variables: dict[str, Any] = {
                "current_time": now.isoformat(),
                "actor_name": request.actor_id,
                "project_context": request.scope,
                "conversation_id": conversation_id,
                "task_goal": request.prompt,
                "parameters_json": json.dumps(
                    request.parameters,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                "context_refs_json": json.dumps(
                    request.context_refs,
                    ensure_ascii=False,
                ),
                "artifact_refs_json": json.dumps(
                    request.artifact_refs,
                    ensure_ascii=False,
                ),
                "artifacts_json": json.dumps(
                    request.artifacts,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                **request.parameters,
            }
            prompt = package.manifest.prompt
            if prompt.system_ref is None:
                raise AgentPackageError("model-backed Agent requires a system prompt")
            selected_names = request.skill.capabilities if request.skill is not None else names
            system_prompt = render_prompt(package.prompts[prompt.system_ref], variables)
            if request.skill is not None:
                system_prompt = (
                    f"{system_prompt}\n\n## Active Skill\n{request.skill.prompt_context}"
                )
            return RunRequest(
                context=RunContext(
                    trace_id=(request.run_id or request.task_id)[:12],
                    actor_id=request.actor_id,
                    conversation_id=request.scope,
                    scope=request.scope,
                    deadline=now + timedelta(seconds=package.manifest.runtime.timeout_seconds),
                    run_id=request.run_id,
                ),
                messages=(
                    {
                        "role": "user",
                        "content": build_task_message(request),
                    },
                ),
                system_prompt=system_prompt,
                capabilities=tuple(self.capabilities.get_schema_list(selected_names)),
                max_rounds=min(
                    package.manifest.runtime.max_turns,
                    package.manifest.budget.max_model_calls,
                ),
                max_tokens=model_policy.max_output_tokens,
            )

        return build


class LangGraphJSONProvider:
    kind = "langgraph"
    name = "json-v1"

    def __init__(
        self,
        gateway: ModelGateway,
        capabilities: CapabilityRegistry,
        skill_resources: SkillResourceLoader | None = None,
    ) -> None:
        self.gateway = gateway
        self.capabilities = capabilities
        self.skill_resources = skill_resources

    def create(self, package: AgentPackage) -> ManagedAgent:
        declaration = package.manifest.capabilities
        names = self.capabilities.resolve_names(declaration.allow, declaration.deny)
        catalog: Mapping[str, Mapping[str, Any]] = {
            name: self.capabilities.get_schema_list((name,))[0] for name in names
        }
        runtime = RoutedModelRuntime(self.gateway, package.manifest.runtime.route)
        return PackageAgentRunner(package, runtime, catalog, self.skill_resources)


class SingleCapabilityProvider:
    kind = "capability"
    name = "single-v1"

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        executor: CapabilityExecutor,
        skill_resources: SkillResourceLoader | None = None,
    ) -> None:
        self.capabilities = capabilities
        self.executor = executor
        self.skill_resources = skill_resources

    def create(self, package: AgentPackage) -> ManagedAgent:
        execution = package.manifest.execution
        capability_name = execution.capability
        if capability_name is None:
            raise AgentPackageError("single capability provider requires a capability")
        names = self.capabilities.resolve_names(
            package.manifest.capabilities.allow,
            package.manifest.capabilities.deny,
        )
        if names != (capability_name,):
            raise AgentPackageError("single capability provider must expose exactly its capability")
        binding = self.capabilities.get(capability_name)
        if binding.capability is None:
            raise AgentPackageError(f"capability {capability_name!r} has no canonical spec")
        if binding.capability.effect is not CapabilityEffect.READ:
            raise AgentPackageError("single capability provider only supports read effects")
        self._validate_source_argument(package, binding.capability.input_schema)
        descriptor = build_agent_descriptor(
            package,
            AgentDescriptor(
                package.manifest.metadata.name,
                package.manifest.metadata.description,
                capabilities=names,
                scopes=package.manifest.routing.scopes,
            ),
        )
        artifact_kind = package.manifest.artifacts[0].kind if package.manifest.artifacts else None
        if len(package.manifest.artifacts) > 1:
            raise AgentPackageError("single capability provider supports at most one artifact")
        delegate = CapabilityAgent(
            descriptor,
            binding.capability,
            self.executor,
            source_argument=execution.source_argument,
            artifact_kind=artifact_kind,
        )
        return ContractAgentDecorator(package, delegate, self.skill_resources)

    @staticmethod
    def _validate_source_argument(package: AgentPackage, input_schema) -> None:
        source_argument = package.manifest.execution.source_argument
        if source_argument is None:
            return
        properties = input_schema.get("properties", {})
        if source_argument not in properties:
            raise AgentPackageError(
                f"source argument {source_argument!r} is not accepted by the capability"
            )
