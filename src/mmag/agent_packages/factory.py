"""Construct governed Agents from behavior-oriented YAML manifests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..agent_system import AgentDescriptor, CapabilityAgent, RuntimeAgent
from ..capabilities import CapabilityEffect
from ..runtimes import RunContext, RunRequest
from ..skill_packages import project_skill_files
from .assets import render_prompt
from .errors import AgentPackageError
from .runtime import (
    ContractAgentDecorator,
    build_agent_descriptor,
    build_task_message,
    expected_result_schema,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..agent_system import AgentRequest, ManagedAgent
    from ..capabilities import CapabilityExecutor, CapabilityRegistry
    from ..governance import ModelGateway, ModelPolicyRegistry
    from .models import AgentPackage


class AgentFactory:
    """Select one of the two stable execution behaviors: agent or direct."""

    def __init__(self, agent_provider: DeepAgentProvider, direct_provider: DirectAgentProvider):
        self.agent_provider = agent_provider
        self.direct_provider = direct_provider

    def create(self, package: AgentPackage) -> ManagedAgent:
        mode = package.manifest.runtime.mode
        if package.manifest.routing.default and mode != "agent":
            raise AgentPackageError("the default Agent must use agent mode")
        if mode == "agent":
            return self.agent_provider.create(package)
        if mode == "direct":
            return self.direct_provider.create(package)
        raise AgentPackageError(f"unknown Agent runtime mode {mode!r}")

    def create_all(self, packages: tuple[AgentPackage, ...]) -> tuple[ManagedAgent, ...]:
        return tuple(self.create(package) for package in packages)


class DeepAgentProvider:
    """Prepare Package-specific inputs for the shared Deep Agents Runtime."""

    def __init__(
        self,
        gateway: ModelGateway,
        capabilities: CapabilityRegistry,
        model_policies: ModelPolicyRegistry,
        *,
        additional_capabilities: tuple[str, ...] = (),
        platform_provenance: Mapping[str, str] | None = None,
    ) -> None:
        self.gateway = gateway
        self.capabilities = capabilities
        self.model_policies = model_policies
        self.additional_capabilities = additional_capabilities
        self.platform_provenance = dict(platform_provenance or {})

    def create(self, package: AgentPackage) -> ManagedAgent:
        names = self.capabilities.resolve_names(
            package.manifest.capabilities.allow,
            package.manifest.capabilities.deny,
            additional_names=self.additional_capabilities,
        )
        descriptor = build_agent_descriptor(
            package,
            AgentDescriptor(
                package.manifest.metadata.name,
                package.manifest.metadata.description,
                capabilities=names,
                scopes=package.manifest.routing.scopes,
            ),
        )
        delegate = RuntimeAgent(
            descriptor,
            self.gateway,
            request_factory=self._request_factory(package, names),
            use_prepared_request=False,
        )
        return ContractAgentDecorator(
            package,
            delegate,
            platform_provenance=self.platform_provenance,
        )

    def _request_factory(self, package: AgentPackage, names: tuple[str, ...]):
        model_policy = self.model_policies.get(package.manifest.model_policy_ref)

        def build(request: AgentRequest, descriptor: AgentDescriptor) -> RunRequest:
            del descriptor
            now = datetime.now(UTC)
            prepared = request.runtime_request
            if prepared is not None and not isinstance(prepared, RunRequest):
                raise AgentPackageError("prepared runtime request has an invalid type")
            conversation_id = (
                prepared.context.conversation_id
                if prepared is not None
                else request.scope.rsplit("/", 1)[-1]
            )
            prompt = package.manifest.prompt
            if prompt.system_ref is None:
                raise AgentPackageError("model-backed Agent requires a system prompt")
            variables: dict[str, Any] = {
                "current_time": now.isoformat(),
                "actor_name": request.actor_id,
                "project_context": request.scope,
                "conversation_id": conversation_id,
                **request.parameters,
            }
            system_prompt = (
                prepared.system_prompt
                if prepared is not None and prepared.system_prompt
                else render_prompt(package.prompts[prompt.system_ref], variables)
            )
            selected_names = request.skill.capabilities if request.skill is not None else names
            capabilities = tuple(self.capabilities.get_schema_list(selected_names))
            runtime_metadata = {
                "task_id": request.task_id,
                "agent_ref": (
                    f"{package.manifest.metadata.name}@{package.manifest.metadata.version}"
                ),
                "skill_ref": request.skill.ref if request.skill is not None else "",
                "policy_ref": package.manifest.policy_ref,
                "route": model_policy.route,
                "model_class": model_policy.model_class,
                "model_policy_ref": model_policy.ref,
                "model_policy_hash": model_policy.sha256,
                "package_hash": package.snapshot.package_hash,
                "capabilities": ",".join(selected_names),
                "execution_profiles": ",".join(package.execution_profiles),
                "max_cost_usd": str(package.manifest.budget.max_cost_usd),
                **self.platform_provenance,
            }
            if prepared is not None:
                return replace(
                    prepared,
                    system_prompt=system_prompt,
                    capabilities=capabilities,
                    max_rounds=min(
                        prepared.max_rounds,
                        package.manifest.runtime.max_turns,
                        package.manifest.budget.max_model_calls,
                    ),
                    max_tool_calls=min(
                        prepared.max_tool_calls,
                        package.manifest.budget.max_tool_calls,
                    ),
                    max_tokens=model_policy.max_output_tokens,
                    temperature=model_policy.temperature,
                    response_schema=expected_result_schema(package, request),
                    skill_files=project_skill_files(package, request),
                    metadata={**prepared.metadata, **runtime_metadata},
                )
            return RunRequest(
                context=RunContext(
                    trace_id=(request.run_id or request.task_id)[:12],
                    actor_id=request.actor_id,
                    conversation_id=conversation_id,
                    scope=request.scope,
                    deadline=now + timedelta(seconds=package.manifest.runtime.timeout_seconds),
                    run_id=request.run_id,
                ),
                messages=({"role": "user", "content": build_task_message(request)},),
                system_prompt=system_prompt,
                capabilities=capabilities,
                max_rounds=min(
                    package.manifest.runtime.max_turns,
                    package.manifest.budget.max_model_calls,
                ),
                max_tool_calls=package.manifest.budget.max_tool_calls,
                max_tokens=model_policy.max_output_tokens,
                temperature=model_policy.temperature,
                response_schema=expected_result_schema(package, request),
                skill_files=project_skill_files(package, request),
                metadata=runtime_metadata,
            )

        return build


class DirectAgentProvider:
    """Execute one deterministic read Capability without a model loop."""

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        executor: CapabilityExecutor,
    ) -> None:
        self.capabilities = capabilities
        self.executor = executor

    def create(self, package: AgentPackage) -> ManagedAgent:
        runtime = package.manifest.runtime
        capability_name = runtime.capability
        if capability_name is None:
            raise AgentPackageError("direct mode requires a capability")
        names = self.capabilities.resolve_names(
            package.manifest.capabilities.allow,
            package.manifest.capabilities.deny,
        )
        if names != (capability_name,):
            raise AgentPackageError("direct mode must expose exactly its capability")
        binding = self.capabilities.get(capability_name)
        if binding.capability is None:
            raise AgentPackageError(f"capability {capability_name!r} has no canonical spec")
        if binding.capability.effect is not CapabilityEffect.READ:
            raise AgentPackageError("direct mode only supports read effects")
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
            raise AgentPackageError("direct mode supports at most one artifact")
        delegate = CapabilityAgent(
            descriptor,
            binding.capability,
            self.executor,
            source_argument=runtime.source_argument,
            artifact_kind=artifact_kind,
        )
        return ContractAgentDecorator(package, delegate)

    @staticmethod
    def _validate_source_argument(package: AgentPackage, input_schema) -> None:
        source_argument = package.manifest.runtime.source_argument
        if source_argument is None:
            return
        if source_argument not in input_schema.get("properties", {}):
            raise AgentPackageError(
                f"source argument {source_argument!r} is not accepted by the capability"
            )
