"""Runtime enforcement for validated Agent Packages."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..agent_system.core import AgentDescriptor, AgentOutput, AgentRequest
from ..governance import GovernanceContext, bind_governance_context, get_governance_context
from ..runtimes import RunContext, RunRequest
from .errors import (
    AgentPackageError,
    InvalidAgentOutputError,
    PromptContractError,
    SchemaContractError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    model_calls: int,
    tool_calls: int,
    cost_usd: float,
) -> dict[str, Any]:
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
        "provenance": package.snapshot.to_dict(),
    }


def _package_governance(
    package: AgentPackage, descriptor: AgentDescriptor, request: AgentRequest
) -> GovernanceContext:
    parent = get_governance_context()
    return GovernanceContext(
        request.actor_id,
        request.scope,
        roles=parent.roles if parent is not None else frozenset(),
        resources=parent.resources if parent is not None else {},
        policy_ref=package.manifest.policy_ref,
        allowed_capabilities=descriptor.capabilities,
    )


def _constrain_runtime_request(package: AgentPackage, runtime_request: Any) -> Any:
    if not isinstance(runtime_request, RunRequest):
        return runtime_request
    package_deadline = datetime.now(UTC) + timedelta(
        seconds=package.manifest.runtime.timeout_seconds
    )
    current_deadline = runtime_request.context.deadline
    deadline = min(current_deadline, package_deadline) if current_deadline else package_deadline
    return replace(
        runtime_request,
        context=replace(runtime_request.context, deadline=deadline),
        max_rounds=min(
            runtime_request.max_rounds,
            package.manifest.runtime.max_turns,
            package.manifest.budget.max_model_calls,
        ),
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

    def __init__(self, package: AgentPackage, delegate: ManagedAgent):
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
        self.descriptor = build_agent_descriptor(package, delegate.descriptor)

    async def run(self, request: AgentRequest) -> AgentOutput:
        input_envelope = _input_envelope(request)
        _validate(
            self.package.schemas[self.package.manifest.input_schema_ref],
            input_envelope,
            direction="input",
        )
        constrained = replace(
            request,
            runtime_request=_constrain_runtime_request(self.package, request.runtime_request),
        )
        with bind_governance_context(
            _package_governance(self.package, self.descriptor, constrained)
        ):
            result = await self.delegate.run(constrained)
        structured = result.structured_result or {"text": result.text}
        artifacts = tuple(result.artifacts)
        _validate_artifacts(self.package, artifacts)
        runtime_result = result.runtime_result
        tool_calls = len(runtime_result.capability_calls) if runtime_result is not None else 1
        cost_usd = runtime_result.usage.cost_usd if runtime_result is not None else 0.0
        budget = self.package.manifest.budget
        if tool_calls > budget.max_tool_calls or cost_usd > budget.max_cost_usd:
            raise AgentPackageError("Agent Package runtime budget exceeded")
        envelope = _output_envelope(
            self.package,
            structured,
            artifacts,
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
    ):
        self.package = package
        self.runtime = runtime
        catalog = capability_catalog or {}
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
        envelope = _input_envelope(request)
        _validate(
            self.package.schemas[self.package.manifest.input_schema_ref],
            envelope,
            direction="input",
        )
        now = datetime.now(UTC)
        variables = {
            "current_time": now.isoformat(),
            "actor_name": request.actor_id,
            "project_context": request.scope,
            "task_goal": request.prompt,
        }
        prompt = self.package.manifest.prompt
        runtime_request = RunRequest(
            context=RunContext(
                trace_id=envelope["run_id"][:12],
                actor_id=request.actor_id,
                conversation_id=request.scope,
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
            system_prompt=_render(self.package.prompts[prompt.system_ref], variables),
            capabilities=self._capabilities,
            max_rounds=min(
                self.package.manifest.runtime.max_turns,
                self.package.manifest.budget.max_model_calls,
            ),
        )
        governance = _package_governance(self.package, self.descriptor, request)
        with bind_governance_context(governance):
            runtime_result = await self.runtime.run(runtime_request)
        model_calls = 1
        try:
            structured, output = self._normalize(runtime_result, model_calls)
        except (json.JSONDecodeError, SchemaContractError, TypeError) as first_error:
            repair_ref = prompt.output_repair_ref
            if repair_ref is None:
                raise InvalidAgentOutputError(str(first_error), direction="output") from first_error
            repair_variables = {
                **variables,
                "invalid_output": runtime_result.text,
                "validation_error": str(first_error),
            }
            repair_request = replace(
                runtime_request,
                messages=(
                    {
                        "role": "user",
                        "content": _render(self.package.prompts[repair_ref], repair_variables),
                    },
                ),
                capabilities=(),
                max_rounds=1,
            )
            with bind_governance_context(governance):
                repaired = await self.runtime.run(repair_request)
            model_calls += 1
            try:
                structured, output = self._normalize(repaired, model_calls)
                runtime_result = repaired
            except (json.JSONDecodeError, SchemaContractError, TypeError) as repair_error:
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

    def _normalize(self, runtime_result, model_calls: int) -> tuple[dict[str, Any], dict[str, Any]]:
        structured = json.loads(runtime_result.text)
        if not isinstance(structured, dict):
            raise TypeError("model result must be a JSON object")
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
