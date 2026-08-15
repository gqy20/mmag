"""Runtime enforcement for validated Agent Packages."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..agent_system.core import AgentDescriptor, AgentOutput, AgentRequest
from ..capabilities import (
    CapabilityContext,
    bind_capability_context,
    get_capability_context,
)
from ..control_plane import MattermostScopeResolver, ScopeKind, scope_resource_id
from ..governance import GovernanceContext, bind_governance_context, get_governance_context
from ..logger import log_context
from ..runtimes import RunRequest, RuntimeStatus
from ..skill_packages import (
    SkillContext,
    bind_skill_context,
    build_skill_provenance,
    validate_skill_contract,
)
from .errors import (
    AgentPackageError,
    PromptContractError,
    SchemaContractError,
)

if TYPE_CHECKING:
    from ..agent_system import ManagedAgent
    from .models import AgentPackage, PromptAsset, SchemaAsset


def _validate(asset: SchemaAsset, value: Any, *, direction: str) -> None:
    _validate_schema(asset.schema, value, direction=direction)


def _validate_schema(schema: Mapping[str, Any], value: Any, *, direction: str) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise SchemaContractError(
            f"{direction} contract failed at {location}: {error.message}",
            direction=direction,
        ) from error


def _validate_agent_output(
    package: AgentPackage,
    request: AgentRequest,
    envelope: Mapping[str, Any],
) -> None:
    asset = package.schemas[package.manifest.result_schema_ref]
    runtime_request = request.runtime_request
    response_schema = (
        runtime_request.response_schema
        if isinstance(runtime_request, RunRequest)
        else None
    )
    if request.skill is None and response_schema is None:
        result = envelope.get("result")
        if isinstance(result, Mapping) and set(result.keys()) <= {"text"}:
            schema = dict(asset.schema)
            properties = dict(schema.get("properties", {}))
            properties["result"] = {"type": "object"}
            schema["properties"] = properties
            _validate_schema(schema, envelope, direction="output")
            return
        _validate(asset, envelope, direction="output")
        return
    if request.skill is not None:
        # A selected Skill owns and already validates the exact result contract. The Agent
        # contract still validates the governed envelope without duplicating every Skill schema.
        schema = dict(asset.schema)
        properties = dict(schema.get("properties", {}))
        properties["result"] = {"type": "object"}
        schema["properties"] = properties
        _validate_schema(schema, envelope, direction="output")
        return
    _validate(asset, envelope, direction="output")


def _render(asset: PromptAsset, variables: Mapping[str, Any]) -> str:
    missing = asset.variables - variables.keys()
    if missing:
        raise PromptContractError(
            f"prompt {asset.ref!r} is missing variables: {', '.join(sorted(missing))}"
        )
    try:
        return asset.content.format_map(variables)
    except (KeyError, ValueError) as error:
        raise PromptContractError(f"prompt {asset.ref!r} could not be rendered: {error}") from error


def build_task_message(request: AgentRequest) -> str:
    """Build the common request envelope once; domain workflow belongs to the active Skill."""
    sections = [f"Goal:\n{request.prompt}"]
    if request.parameters:
        sections.append(
            "Parameters:\n"
            + json.dumps(request.parameters, ensure_ascii=False, sort_keys=True, default=str)
        )
    if request.context_refs:
        sections.append("Context references:\n" + json.dumps(request.context_refs, ensure_ascii=False))
    if request.artifact_refs:
        sections.append(
            "Artifact references:\n" + json.dumps(request.artifact_refs, ensure_ascii=False)
        )
    return "\n\n".join(sections)


def build_agent_descriptor(
    package: AgentPackage, base: AgentDescriptor | None = None
) -> AgentDescriptor:
    manifest = package.manifest
    return AgentDescriptor(
        name=manifest.metadata.name,
        description=manifest.metadata.description,
        intents=manifest.accepted_intents,
        capabilities=base.capabilities if base else manifest.capabilities.allow,
        permissions=base.permissions if base else (),
        scopes=base.scopes if base else manifest.routing.scopes,
        max_cost_usd=manifest.budget.max_cost_usd,
        healthy=base.healthy if base else True,
        is_default=manifest.routing.default,
        routing_priority=manifest.routing.priority,
        routing_keywords=manifest.routing.keywords,
        requires_url=manifest.routing.requires_url,
    )


def _input_envelope(request: AgentRequest) -> dict[str, Any]:
    run_id = request.run_id or uuid.uuid4().hex
    return {
        "task_id": request.task_id or f"task-{run_id[:12]}",
        "run_id": run_id,
        "intent": request.intent,
        "actor": {"id": request.actor_id},
        "scope": {"resource": request.scope},
        "goal": request.prompt,
        "parameters": dict(request.parameters),
        "context_refs": list(request.context_refs),
        "artifact_refs": list(request.artifact_refs),
    }


def _output_envelope(
    package: AgentPackage,
    result: Mapping[str, Any],
    artifacts: tuple[Mapping[str, Any], ...],
    *,
    request: AgentRequest,
    model_calls: int,
    tool_calls: int,
    cost_usd: float,
    platform_provenance: Mapping[str, str],
) -> dict[str, Any]:
    provenance = package.snapshot.to_dict()
    provenance.update(platform_provenance)
    if request.skill is not None:
        provenance.update(request.skill.provenance)
    return {
        "status": "succeeded",
        "agent": package.manifest.metadata.name,
        "result": dict(result),
        "artifacts": [dict(artifact) for artifact in artifacts],
        "sources": [],
        "warnings": [],
        "usage": {
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
        },
        "provenance": provenance,
    }


def _package_governance(
    package: AgentPackage, descriptor: AgentDescriptor, request: AgentRequest
) -> GovernanceContext:
    parent = get_governance_context()
    allowed = descriptor.capabilities
    return GovernanceContext(
        request.actor_id,
        request.scope,
        roles=parent.roles if parent is not None else frozenset(),
        resources=parent.resources if parent is not None else {},
        policy_ref=package.manifest.policy_ref,
        allowed_capabilities=allowed,
    )


def _package_capability_context(
    package: AgentPackage,
    request: AgentRequest,
    runtime_request: Any,
    allowed_capabilities: tuple[str, ...],
) -> CapabilityContext:
    parent = get_capability_context()
    runtime_context = runtime_request.context if isinstance(runtime_request, RunRequest) else None
    return CapabilityContext(
        trace_id=(
            parent.trace_id
            if parent is not None
            else runtime_context.trace_id
            if runtime_context is not None
            else request.run_id
        ),
        actor_id=request.actor_id,
        conversation_id=(
            parent.conversation_id
            if parent is not None
            else runtime_context.conversation_id
            if runtime_context is not None
            else scope_resource_id(request.scope)
        ),
        message_id=parent.message_id if parent is not None else "",
        message=parent.message if parent is not None else request.prompt,
        scope=(
            runtime_context.scope
            if runtime_context is not None
            else parent.scope
            if parent is not None
            else request.scope
        ),
        allowed_capabilities=frozenset(allowed_capabilities),
        run_id=(runtime_context.run_id if runtime_context is not None else request.run_id),
        allowed_execution_profiles=frozenset(package.execution_profiles),
        installation_id=(
            runtime_context.installation_id
            if runtime_context is not None
            else parent.installation_id
            if parent is not None
            else ""
        ),
        tenant_id=(
            runtime_context.tenant_id
            if runtime_context is not None
            else parent.tenant_id
            if parent is not None
            else ""
        ),
        scope_kind=(
            runtime_context.scope_kind
            if runtime_context is not None
            else parent.scope_kind
            if parent is not None
            else ""
        ),
        owner_id=(
            runtime_context.owner_id
            if runtime_context is not None
            else parent.owner_id
            if parent is not None
            else ""
        ),
        team_id=(
            runtime_context.team_id
            if runtime_context is not None
            else parent.team_id
            if parent is not None
            else ""
        ),
        channel_type=(
            runtime_context.channel_type
            if runtime_context is not None
            else parent.channel_type
            if parent is not None
            else ""
        ),
    )


def _constrain_runtime_request(
    package: AgentPackage,
    runtime_request: Any,
    allowed_capabilities: tuple[str, ...] | None = None,
) -> Any:
    if not isinstance(runtime_request, RunRequest):
        return runtime_request
    package_deadline = datetime.now(UTC) + timedelta(
        seconds=package.manifest.runtime.timeout_seconds
    )
    current_deadline = runtime_request.context.deadline
    deadline = min(current_deadline, package_deadline) if current_deadline else package_deadline
    capabilities = runtime_request.capabilities
    if allowed_capabilities is not None:
        allowed = set(allowed_capabilities)
        capabilities = tuple(
            capability for capability in capabilities if capability.get("name") in allowed
        )
    return replace(
        runtime_request,
        context=replace(runtime_request.context, deadline=deadline),
        max_rounds=min(
            runtime_request.max_rounds,
            package.manifest.runtime.max_turns,
            package.manifest.budget.max_model_calls,
        ),
        max_tool_calls=min(
            runtime_request.max_tool_calls,
            package.manifest.budget.max_tool_calls,
        ),
        capabilities=capabilities,
    )


def _validate_skill_invocation(
    package: AgentPackage,
    descriptor: AgentDescriptor,
    request: AgentRequest,
) -> None:
    invocation = request.skill
    if invocation is None:
        return
    try:
        skill = package.skills[invocation.ref]
    except KeyError as error:
        raise AgentPackageError(
            f"Skill {invocation.ref!r} is not bound to Agent {descriptor.name!r}"
        ) from error
    platform_provenance = {
        key: value
        for key, value in invocation.provenance.items()
        if not key.startswith("personal_skill_")
    }
    if platform_provenance != build_skill_provenance(package, skill):
        raise AgentPackageError(f"Skill {invocation.ref!r} provenance is invalid")
    if invocation.personal_ref:
        if not invocation.personal_ref.startswith("pskill://"):
            raise AgentPackageError("Personal Skill ref is invalid")
        if not invocation.personal_instruction or len(invocation.personal_instruction) > 12_000:
            raise AgentPackageError("Personal Skill instruction is invalid")
        if len(invocation.personal_template) > 20_000:
            raise AgentPackageError("Personal Skill template is invalid")
        try:
            _, _, kind, owner_id = MattermostScopeResolver.parse(request.scope)
        except ValueError as error:
            raise AgentPackageError("Personal Skill scope is invalid") from error
        if kind is not ScopeKind.PERSONAL or owner_id != request.actor_id:
            raise AgentPackageError("Personal Skill cannot escape its owner scope")
    declared = skill.manifest.capabilities
    required = set(declared.required)
    selected = set(invocation.capabilities)
    skill_capabilities = required | set(declared.optional)
    if not required <= selected or not selected <= skill_capabilities:
        raise AgentPackageError(f"Skill {invocation.ref!r} has an invalid capability projection")
    if not selected <= set(descriptor.capabilities):
        raise AgentPackageError(f"Skill {invocation.ref!r} expands Agent capabilities")
    validate_skill_contract(
        skill.schemas[skill.manifest.input_schema_ref],
        {
            "intent": request.intent,
            "goal": request.prompt,
            "parameters": dict(request.parameters),
        },
        direction="input",
    )


def _validate_skill_output(package: AgentPackage, request: AgentRequest, result: Any) -> None:
    if request.skill is None:
        return
    skill = package.skills[request.skill.ref]
    validate_skill_contract(
        skill.schemas[skill.manifest.output_schema_ref],
        result,
        direction="output",
    )


def expected_result_schema(package: AgentPackage, request: AgentRequest) -> Mapping[str, Any]:
    if request.skill is not None:
        skill = package.skills[request.skill.ref]
        return skill.schemas[skill.manifest.output_schema_ref].schema
    envelope_schema = package.schemas[package.manifest.result_schema_ref].schema
    properties = envelope_schema.get("properties", {})
    result_schema = properties.get("result") if isinstance(properties, Mapping) else None
    if isinstance(result_schema, Mapping) and isinstance(result_schema.get("$ref"), str):
        reference = result_schema["$ref"]
        if reference.startswith("#/$defs/"):
            definitions = envelope_schema.get("$defs", {})
            name = reference.removeprefix("#/$defs/")
            result_schema = definitions.get(name) if isinstance(definitions, Mapping) else None
    return result_schema if isinstance(result_schema, Mapping) else envelope_schema


def _create_skill_context(
    package: AgentPackage,
    request: AgentRequest,
) -> SkillContext | None:
    if request.skill is None:
        return None
    return SkillContext(
        package.skills[request.skill.ref],
        personal_ref=request.skill.personal_ref,
        personal_hash=str(request.skill.provenance.get("personal_skill_hash") or ""),
    )


def _validate_artifacts(package: AgentPackage, artifacts: tuple[Mapping[str, Any], ...]) -> None:
    declarations = {item.kind: item for item in package.manifest.artifacts}
    for artifact in artifacts:
        kind = artifact.get("kind")
        if not isinstance(kind, str):
            raise SchemaContractError("output artifact is missing kind", direction="output")
        declaration = declarations.get(kind)
        if declaration is None:
            raise SchemaContractError(
                f"output contains undeclared artifact kind {kind!r}", direction="output"
            )
        _validate(package.schemas[declaration.schema_ref], artifact, direction=f"artifact:{kind}")


class ContractAgentDecorator:
    """Apply Package contracts around a deterministic or prepared Runtime Agent."""

    def __init__(
        self,
        package: AgentPackage,
        delegate: ManagedAgent,
        *,
        platform_provenance: Mapping[str, str] | None = None,
    ):
        allowed = package.manifest.capabilities.allow
        denied_patterns = package.manifest.capabilities.deny
        undeclared = {
            name
            for name in delegate.descriptor.capabilities
            if not any(fnmatch(name, pattern) for pattern in allowed)
        }
        denied = {
            name
            for name in delegate.descriptor.capabilities
            if any(fnmatch(name, pattern) for pattern in denied_patterns)
        }
        if undeclared or denied:
            names = ", ".join(sorted(undeclared | denied))
            raise AgentPackageError(f"delegate exposes capabilities outside its manifest: {names}")
        self.package = package
        self.delegate = delegate
        self.platform_provenance = MappingProxyType(dict(platform_provenance or {}))
        self.descriptor = build_agent_descriptor(package, delegate.descriptor)

    async def run(self, request: AgentRequest) -> AgentOutput:
        _validate_skill_invocation(self.package, self.descriptor, request)
        input_envelope = _input_envelope(request)
        _validate(
            self.package.schemas[self.package.manifest.input_schema_ref],
            input_envelope,
            direction="input",
        )
        skill_capabilities = (
            request.skill.capabilities if request.skill is not None else None
        )
        constrained = replace(
            request,
            runtime_request=_constrain_runtime_request(
                self.package,
                request.runtime_request,
                skill_capabilities,
            ),
        )
        skill_context = _create_skill_context(
            self.package,
            constrained,
        )
        skill_scope = (
            bind_skill_context(skill_context)
            if skill_context is not None
            else nullcontext()
        )
        allowed_capabilities = (
            self.descriptor.capabilities
            if skill_capabilities is None
            else skill_capabilities
        )
        with (
            log_context.bind(
                trace_id=(
                    constrained.runtime_request.context.trace_id
                    if constrained.runtime_request is not None
                    else request.run_id
                ),
                task_id=request.task_id,
                run_id=(
                    constrained.runtime_request.context.run_id
                    if constrained.runtime_request is not None
                    else request.run_id
                ),
                actor_id=request.actor_id,
                conversation_id=(
                    constrained.runtime_request.context.conversation_id
                    if constrained.runtime_request is not None
                    else ""
                ),
                thread_id=(
                    constrained.runtime_request.context.run_id
                    if constrained.runtime_request is not None
                    else request.run_id
                ),
                agent_ref=(
                    f"{self.package.manifest.metadata.name}@"
                    f"{self.package.manifest.metadata.version}"
                ),
                skill_ref=request.skill.ref if request.skill is not None else "",
                policy_ref=self.package.manifest.policy_ref,
            ),
            skill_scope,
            bind_capability_context(
                _package_capability_context(
                    self.package,
                    constrained,
                    constrained.runtime_request,
                    allowed_capabilities,
                )
            ),
            bind_governance_context(
                _package_governance(self.package, self.descriptor, constrained)
            ),
        ):
            result = await self.delegate.run(constrained)
        runtime_result = result.runtime_result
        if runtime_result is not None and runtime_result.status is RuntimeStatus.WAITING_APPROVAL:
            return replace(result, agent_name=self.descriptor.name)
        structured = result.result
        if structured is None:
            raise AgentPackageError("Agent Runtime returned no structured result")
        _validate_skill_output(self.package, request, structured)
        artifacts = tuple(result.artifacts)
        _validate_artifacts(self.package, artifacts)
        tool_calls = len(runtime_result.capability_calls) if runtime_result is not None else 1
        cost_usd = runtime_result.usage.cost_usd if runtime_result is not None else 0.0
        budget = self.package.manifest.budget
        if tool_calls > budget.max_tool_calls or cost_usd > budget.max_cost_usd:
            raise AgentPackageError("Agent Package runtime budget exceeded")
        envelope = _output_envelope(
            self.package,
            structured,
            artifacts,
            request=request,
            model_calls=(runtime_result.usage.model_calls if runtime_result is not None else 0),
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            platform_provenance=self.platform_provenance,
        )
        _validate_agent_output(self.package, request, envelope)
        return replace(result, agent_name=self.descriptor.name, envelope=envelope)
