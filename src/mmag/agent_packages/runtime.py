"""Runtime enforcement for validated Agent Packages."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..agent_system.core import AgentDescriptor, AgentOutput, AgentRequest
from ..capabilities import (
    CapabilityContext,
    bind_capability_context,
    get_capability_context,
)
from ..governance import GovernanceContext, bind_governance_context, get_governance_context
from ..runtimes import RunContext, RunRequest, RuntimeStatus
from ..runtimes.base import thaw
from ..skill_packages import (
    SkillContractError,
    SkillResourceLoader,
    SkillResourceSession,
    bind_skill_resource_session,
    build_skill_provenance,
    build_skill_resource_catalog,
    validate_skill_contract,
)
from .errors import (
    AgentPackageError,
    InvalidAgentOutputError,
    PromptContractError,
    SchemaContractError,
)

if TYPE_CHECKING:
    from ..agent_system import ManagedAgent
    from ..runtimes import AgentRuntime
    from .models import AgentPackage, PromptAsset, SchemaAsset


def _validate(asset: SchemaAsset, value: Any, *, direction: str) -> None:
    try:
        Draft202012Validator(asset.schema).validate(value)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise SchemaContractError(
            f"{direction} contract failed at {location}: {error.message}",
            direction=direction,
        ) from error


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
    resource_provenance: Mapping[str, str] | None = None,
    model_calls: int,
    tool_calls: int,
    cost_usd: float,
) -> dict[str, Any]:
    provenance = package.snapshot.to_dict()
    if request.skill is not None:
        provenance.update(request.skill.provenance)
    if resource_provenance is not None:
        provenance.update(resource_provenance)
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
    allowed = request.skill.capabilities if request.skill is not None else descriptor.capabilities
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
            else request.scope.rsplit("/", 1)[-1]
        ),
        message_id=parent.message_id if parent is not None else "",
        message=parent.message if parent is not None else request.prompt,
        scope=runtime_context.scope if runtime_context is not None else request.scope,
        allowed_capabilities=frozenset(allowed_capabilities),
        run_id=(runtime_context.run_id if runtime_context is not None else request.run_id),
        allowed_execution_profiles=frozenset(package.execution_profiles),
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
    expected_hash = skill.snapshot.instruction_hash
    actual_hash = hashlib.sha256(invocation.instructions.encode()).hexdigest()
    expected_catalog = build_skill_resource_catalog(skill)
    if (
        actual_hash != expected_hash
        or invocation.resource_catalog != expected_catalog
        or invocation.provenance != build_skill_provenance(package, skill)
    ):
        raise AgentPackageError(f"Skill {invocation.ref!r} provenance is invalid")
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


def _expected_result_schema(package: AgentPackage, request: AgentRequest) -> Mapping[str, Any]:
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


def _schema_json(schema: Mapping[str, Any]) -> str:
    return json.dumps(
        thaw(schema),
        ensure_ascii=False,
        sort_keys=True,
    )


def _bind_conversation_resource(
    capabilities: tuple[Mapping[str, Any], ...], conversation_id: str
) -> tuple[Mapping[str, Any], ...]:
    """Narrow model-visible channel arguments without changing authorization policy."""
    projected: list[Mapping[str, Any]] = []
    for capability in capabilities:
        narrowed = thaw(capability)
        schema = narrowed.get("input_schema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        channel = properties.get("channel_id") if isinstance(properties, dict) else None
        if isinstance(channel, dict):
            channel["const"] = conversation_id
            channel["description"] = "Trusted originating conversation ID; use this exact value."
        projected.append(narrowed)
    return tuple(projected)


def _create_resource_session(
    package: AgentPackage,
    request: AgentRequest,
    loader: SkillResourceLoader,
) -> SkillResourceSession | None:
    if request.skill is None:
        return None
    return loader.create_session(package.skills[request.skill.ref])


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
        skill_resources: SkillResourceLoader | None = None,
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
        self.skill_resources = skill_resources or SkillResourceLoader()
        self.descriptor = build_agent_descriptor(package, delegate.descriptor)

    async def run(self, request: AgentRequest) -> AgentOutput:
        _validate_skill_invocation(self.package, self.descriptor, request)
        input_envelope = _input_envelope(request)
        _validate(
            self.package.schemas[self.package.manifest.input_schema_ref],
            input_envelope,
            direction="input",
        )
        constrained = replace(
            request,
            runtime_request=_constrain_runtime_request(
                self.package,
                request.runtime_request,
                request.skill.capabilities if request.skill is not None else None,
            ),
        )
        resource_session = _create_resource_session(
            self.package,
            constrained,
            self.skill_resources,
        )
        resource_context = (
            bind_skill_resource_session(resource_session)
            if resource_session is not None
            else nullcontext()
        )
        allowed_capabilities = (
            constrained.skill.capabilities
            if constrained.skill is not None
            else self.descriptor.capabilities
        )
        with (
            resource_context,
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
        structured = result.structured_result or {"text": result.text}
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
            resource_provenance=(
                resource_session.provenance() if resource_session is not None else None
            ),
            model_calls=1 if runtime_result is not None else 0,
            tool_calls=tool_calls,
            cost_usd=cost_usd,
        )
        _validate(
            self.package.schemas[self.package.manifest.result_schema_ref],
            envelope,
            direction="output",
        )
        return replace(result, agent_name=self.descriptor.name, envelope=envelope)


class PackageAgentRunner:
    """Render, execute and validate a model-backed Agent Package.

    The model returns only the package-specific ``result`` object. MMAG owns the
    surrounding envelope and allows at most one structure-repair invocation.
    """

    def __init__(
        self,
        package: AgentPackage,
        runtime: AgentRuntime,
        capability_catalog: Mapping[str, Mapping[str, Any]] | None = None,
        skill_resources: SkillResourceLoader | None = None,
    ):
        self.package = package
        self.runtime = runtime
        self.skill_resources = skill_resources or SkillResourceLoader()
        catalog = capability_catalog or {}
        self._capability_catalog = dict(catalog)
        self.descriptor = build_agent_descriptor(
            package,
            AgentDescriptor(
                package.manifest.metadata.name,
                package.manifest.metadata.description,
                capabilities=tuple(catalog),
                scopes=package.manifest.routing.scopes,
            ),
        )
        missing = set(package.manifest.capabilities.allow) - catalog.keys()
        if missing:
            raise AgentPackageError(f"unknown allowed capabilities: {', '.join(sorted(missing))}")
        self._capabilities = tuple(catalog[name] for name in package.manifest.capabilities.allow)

    async def run(self, request: AgentRequest) -> AgentOutput:
        _validate_skill_invocation(self.package, self.descriptor, request)
        envelope = _input_envelope(request)
        _validate(
            self.package.schemas[self.package.manifest.input_schema_ref],
            envelope,
            direction="input",
        )
        now = datetime.now(UTC)
        prepared = request.runtime_request
        conversation_id = (
            prepared.context.conversation_id
            if isinstance(prepared, RunRequest)
            else request.scope.rsplit("/", 1)[-1]
        )
        variables = {
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
        }
        prompt = self.package.manifest.prompt
        capabilities = self._capabilities
        if request.skill is not None:
            capabilities = tuple(
                self._capability_catalog[name] for name in request.skill.capabilities
            )
        capabilities = _bind_conversation_resource(capabilities, conversation_id)
        system_prompt = _render(self.package.prompts[prompt.system_ref], variables)
        if request.skill is not None:
            system_prompt = f"{system_prompt}\n\n## Active Skill\n{request.skill.prompt_context}"
        runtime_request = RunRequest(
            context=RunContext(
                trace_id=envelope["run_id"][:12],
                actor_id=request.actor_id,
                conversation_id=conversation_id,
                scope=request.scope,
                deadline=now + timedelta(seconds=self.package.manifest.runtime.timeout_seconds),
                run_id=envelope["run_id"],
            ),
            messages=(
                {
                    "role": "user",
                    "content": _render(self.package.prompts[prompt.task_ref], variables),
                },
            ),
            system_prompt=system_prompt,
            capabilities=capabilities,
            max_rounds=min(
                self.package.manifest.runtime.max_turns,
                self.package.manifest.budget.max_model_calls,
            ),
        )
        governance = _package_governance(self.package, self.descriptor, request)
        resource_session = _create_resource_session(
            self.package,
            request,
            self.skill_resources,
        )
        resource_context = (
            bind_skill_resource_session(resource_session)
            if resource_session is not None
            else nullcontext()
        )
        allowed_names = tuple(
            str(capability.get("name")) for capability in capabilities if capability.get("name")
        )
        capability_context = _package_capability_context(
            self.package,
            request,
            runtime_request,
            allowed_names,
        )
        with (
            resource_context,
            bind_capability_context(capability_context),
            bind_governance_context(governance),
        ):
            runtime_result = await self.runtime.run(runtime_request)
        if runtime_result.status is RuntimeStatus.WAITING_APPROVAL:
            return AgentOutput(
                "",
                self.descriptor.name,
                runtime_result=runtime_result,
            )
        model_calls = 1
        try:
            structured, output = self._normalize(
                runtime_result,
                model_calls,
                request,
                resource_session,
            )
        except (
            json.JSONDecodeError,
            SchemaContractError,
            SkillContractError,
            TypeError,
        ) as first_error:
            repair_ref = prompt.output_repair_ref
            if repair_ref is None:
                raise InvalidAgentOutputError(str(first_error), direction="output") from first_error
            repair_variables = {
                **variables,
                "invalid_output": runtime_result.text,
                "validation_error": str(first_error),
            }
            repair_content = _render(self.package.prompts[repair_ref], repair_variables)
            repair_content = (
                f"{repair_content}\n\nExact required JSON Schema:\n"
                f"{_schema_json(_expected_result_schema(self.package, request))}"
            )
            repair_request = replace(
                runtime_request,
                messages=(
                    {
                        "role": "user",
                        "content": repair_content,
                    },
                ),
                capabilities=(),
                max_rounds=1,
            )
            resource_context = (
                bind_skill_resource_session(resource_session)
                if resource_session is not None
                else nullcontext()
            )
            with (
                resource_context,
                bind_capability_context(capability_context),
                bind_governance_context(governance),
            ):
                repaired = await self.runtime.run(repair_request)
            model_calls += 1
            try:
                structured, output = self._normalize(
                    repaired,
                    model_calls,
                    request,
                    resource_session,
                )
                runtime_result = repaired
            except (
                json.JSONDecodeError,
                SchemaContractError,
                SkillContractError,
                TypeError,
            ) as repair_error:
                raise InvalidAgentOutputError(
                    str(repair_error), direction="output"
                ) from repair_error
        return AgentOutput(
            json.dumps(structured, ensure_ascii=False),
            self.descriptor.name,
            tuple(dict(artifact) for artifact in runtime_result.artifacts),
            dict(structured),
            output,
        )

    def _normalize(
        self,
        runtime_result,
        model_calls: int,
        request: AgentRequest,
        resource_session: SkillResourceSession | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        structured = json.loads(runtime_result.text)
        if not isinstance(structured, dict):
            raise TypeError("model result must be a JSON object")
        _validate_skill_output(self.package, request, structured)
        artifacts = tuple(runtime_result.artifacts)
        _validate_artifacts(self.package, artifacts)
        tool_calls = len(runtime_result.capability_calls)
        usage = runtime_result.usage
        budget = self.package.manifest.budget
        if tool_calls > budget.max_tool_calls or usage.cost_usd > budget.max_cost_usd:
            raise AgentPackageError("Agent Package runtime budget exceeded")
        output = _output_envelope(
            self.package,
            structured,
            artifacts,
            request=request,
            resource_provenance=(
                resource_session.provenance() if resource_session is not None else None
            ),
            model_calls=model_calls,
            tool_calls=tool_calls,
            cost_usd=usage.cost_usd,
        )
        _validate(
            self.package.schemas[self.package.manifest.result_schema_ref],
            output,
            direction="output",
        )
        return structured, output
