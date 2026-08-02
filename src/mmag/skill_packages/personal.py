"""Resolve and compile one owner-scoped Personal Skill overlay."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import unquote

from .errors import SkillResolutionError

if TYPE_CHECKING:
    from ..agent_system import AgentRequest, SkillInvocation
    from ..control_plane import PersonalSkill, PersonalSkillStore


class PersonalSkillResolver:
    def __init__(self, store: PersonalSkillStore) -> None:
        self.store = store

    def resolve(
        self,
        request: AgentRequest,
        *,
        agent_name: str,
        base_skill_ref: str,
    ) -> PersonalSkill | None:
        requested = request.requested_personal_skill
        identity = self._identity(request, required=bool(requested))
        if identity is None:
            return None
        installation_id, tenant_id, owner_id = identity
        if requested:
            skill = self.requested(request)
            self._validate_binding(skill, installation_id, tenant_id, agent_name, base_skill_ref)
            return skill
        candidates = [
            skill
            for skill in self.store.list_active(
                installation_id=installation_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if skill.auto_select
            and self._binding_matches(skill, agent_name, base_skill_ref)
            and self._activation_score(skill, request) > 0
        ]
        if not candidates:
            return None
        ranked = sorted(
            candidates,
            key=lambda skill: (self._activation_score(skill, request), skill.revision),
            reverse=True,
        )
        if len(ranked) > 1 and self._activation_score(ranked[0], request) == self._activation_score(
            ranked[1], request
        ):
            return None
        return ranked[0]

    def requested(self, request: AgentRequest) -> PersonalSkill:
        identity = self._identity(request, required=True)
        assert identity is not None
        installation_id, tenant_id, owner_id = identity
        try:
            skill = self.store.get(request.requested_personal_skill, owner_id=owner_id)
        except (KeyError, ValueError) as error:
            raise SkillResolutionError("requested Personal Skill is unavailable") from error
        if skill.status.value != "active":
            raise SkillResolutionError("requested Personal Skill is not active")
        if skill.installation_id != installation_id or skill.tenant_id != tenant_id:
            raise SkillResolutionError("requested Personal Skill belongs to another tenant")
        return skill

    @staticmethod
    def _identity(
        request: AgentRequest, *, required: bool
    ) -> tuple[str, str, str] | None:
        try:
            installation_id, tenant_id, kind, owner_id = _parse_personal_scope(request.scope)
        except ValueError:
            if required:
                raise SkillResolutionError(
                    "Personal Skill requires a valid Mattermost Personal Scope"
                ) from None
            return None
        if kind != "personal" or owner_id != request.actor_id:
            if required:
                raise SkillResolutionError(
                    "Personal Skill is available only to its owner in DM"
                )
            return None
        return installation_id, tenant_id, owner_id

    @staticmethod
    def _binding_matches(skill: PersonalSkill, agent_name: str, base_skill_ref: str) -> bool:
        return skill.base_skill_ref == base_skill_ref and (
            not skill.preferred_agent or skill.preferred_agent == agent_name
        )

    @classmethod
    def _validate_binding(
        cls,
        skill: PersonalSkill,
        installation_id: str,
        tenant_id: str,
        agent_name: str,
        base_skill_ref: str,
    ) -> None:
        if skill.status.value != "active":
            raise SkillResolutionError("requested Personal Skill is not active")
        if skill.installation_id != installation_id or skill.tenant_id != tenant_id:
            raise SkillResolutionError("requested Personal Skill belongs to another tenant")
        if not cls._binding_matches(skill, agent_name, base_skill_ref):
            raise SkillResolutionError(
                "requested Personal Skill is incompatible with the selected Agent and Skill"
            )

    @staticmethod
    def _activation_score(skill: PersonalSkill, request: AgentRequest) -> int:
        intent = request.intent.lower()
        prompt = request.prompt.lower()
        return 10 * int(intent in {item.lower() for item in skill.activation_intents}) + sum(
            keyword.lower() in prompt for keyword in skill.activation_keywords
        )


def compile_personal_skill(
    invocation: SkillInvocation,
    skill: PersonalSkill,
) -> SkillInvocation:
    provenance = dict(invocation.provenance)
    provenance.update(
        {
            "personal_skill_ref": skill.ref,
            "personal_skill_hash": skill.sha256,
            "personal_skill_base_ref": skill.base_skill_ref,
        }
    )
    return replace(
        invocation,
        provenance=MappingProxyType(provenance),
        personal_ref=skill.ref,
        personal_instruction=skill.instruction,
        personal_template=skill.template,
    )


def _parse_personal_scope(scope_id: str) -> tuple[str, str, str, str]:
    parts = scope_id.split(":")
    if len(parts) != 5 or parts[0] != "mattermost" or parts[3] not in {"usr", "chn"}:
        raise ValueError("invalid Mattermost scope")
    return (
        unquote(parts[1]),
        unquote(parts[2]),
        "personal" if parts[3] == "usr" else "channel",
        unquote(parts[4]),
    )
