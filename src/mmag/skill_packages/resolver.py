"""Resolve one governed Skill after Agent routing."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..agent_system import SkillInvocation
from .errors import SkillContractError, SkillResolutionError
from .loader import load_skill_instructions
from .resources import build_skill_resource_catalog

if TYPE_CHECKING:
    from ..agent_packages import AgentPackage
    from ..agent_system import AgentRequest
    from ..capabilities import CapabilityRegistry
    from .models import SkillPackage, SkillSchemaAsset
    from .registry import SkillPackageRegistry


def validate_skill_contract(
    asset: SkillSchemaAsset,
    value: Any,
    *,
    direction: str,
) -> None:
    try:
        Draft202012Validator(asset.schema).validate(value)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise SkillContractError(
            f"Skill {direction} contract failed at {location}: {error.message}",
            direction=direction,
        ) from error


class SkillResolver:
    """Select only Skills explicitly bound to the already-selected Agent."""

    def __init__(
        self,
        registry: SkillPackageRegistry,
        capabilities: CapabilityRegistry,
    ) -> None:
        self.registry = registry
        self.capabilities = capabilities

    def resolve(
        self,
        package: AgentPackage,
        request: AgentRequest,
        agent_capabilities: tuple[str, ...],
    ) -> SkillInvocation | None:
        allowed = package.manifest.skills.allow
        candidates = [self.registry.get(ref) for ref in allowed]
        requested = request.requested_skill
        if requested:
            selected = self._requested(candidates, requested)
        else:
            matches = [skill for skill in candidates if self._matches(skill, request)]
            if not matches:
                return None
            selected = max(matches, key=lambda skill: self._score(skill, request))
        return self._invocation(selected, request, agent_capabilities)

    @staticmethod
    def _requested(candidates: list[SkillPackage], requested: str) -> SkillPackage:
        matches = [
            skill
            for skill in candidates
            if requested in {skill.manifest.metadata.name, skill.manifest.metadata.ref}
        ]
        if len(matches) != 1:
            raise SkillResolutionError(
                f"requested Skill {requested!r} is not uniquely allowed by the Agent"
            )
        return matches[0]

    @staticmethod
    def _matches(skill: SkillPackage, request: AgentRequest) -> bool:
        activation = skill.manifest.activation
        intent_match = request.intent.lower() in {item.lower() for item in activation.intents}
        keyword_match = any(item.lower() in request.prompt.lower() for item in activation.keywords)
        return intent_match or keyword_match

    @staticmethod
    def _score(skill: SkillPackage, request: AgentRequest) -> tuple[int, int, int, str]:
        activation = skill.manifest.activation
        exact = int(request.intent.lower() in {item.lower() for item in activation.intents})
        keywords = sum(item.lower() in request.prompt.lower() for item in activation.keywords)
        return activation.priority, exact, keywords, skill.manifest.metadata.ref

    def _invocation(
        self,
        skill: SkillPackage,
        request: AgentRequest,
        agent_capabilities: tuple[str, ...],
    ) -> SkillInvocation:
        declared = skill.manifest.capabilities
        agent_allowed = set(agent_capabilities)
        deployed = set(self.capabilities.names())
        missing = set(declared.required) - agent_allowed
        unavailable = set(declared.required) - deployed
        if missing or unavailable:
            names = ", ".join(sorted(missing | unavailable))
            raise SkillResolutionError(
                f"Skill {skill.manifest.metadata.ref!r} lacks required capabilities: {names}"
            )
        effective = tuple(
            name
            for name in (*declared.required, *declared.optional)
            if name in agent_allowed and name in deployed
        )
        input_asset = skill.schemas[skill.manifest.input_schema_ref]
        validate_skill_contract(
            input_asset,
            {
                "intent": request.intent,
                "goal": request.prompt,
                "parameters": dict(request.parameters),
            },
            direction="input",
        )
        return SkillInvocation(
            ref=skill.manifest.metadata.ref,
            instructions=load_skill_instructions(skill),
            resource_catalog=build_skill_resource_catalog(skill),
            capabilities=effective,
            provenance=MappingProxyType(skill.snapshot.to_dict()),
        )
