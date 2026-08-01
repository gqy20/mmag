"""Atomic loading of flat, self-versioned Agent Packages."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from .errors import ManifestValidationError
from .loader import AgentPackageLoader

if TYPE_CHECKING:
    from pathlib import Path

    from ..execution import ExecutionProfileRegistry
    from ..governance import ModelPolicyRegistry, PolicyRegistry
    from ..skill_packages import SkillPackageRegistry
    from .models import AgentPackage


class AgentPackageRegistry:
    def __init__(
        self,
        loader: AgentPackageLoader | None = None,
        *,
        policy_registry: PolicyRegistry | None = None,
        model_policy_registry: ModelPolicyRegistry | None = None,
        skill_registry: SkillPackageRegistry | None = None,
        execution_profile_registry: ExecutionProfileRegistry | None = None,
    ) -> None:
        self.loader = loader or AgentPackageLoader()
        self.policy_registry = policy_registry
        self.model_policy_registry = model_policy_registry
        self.skill_registry = skill_registry
        self.execution_profile_registry = execution_profile_registry
        self._packages: dict[str, AgentPackage] = {}

    def load_directory(self, root: Path) -> tuple[AgentPackage, ...]:
        staged_packages: dict[str, AgentPackage] = {}
        loaded: list[AgentPackage] = []
        for agent_root in sorted(path for path in root.iterdir() if path.is_dir()):
            package = self.loader.load(agent_root)
            name = package.manifest.metadata.name
            if name != agent_root.name:
                raise ManifestValidationError(
                    f"Agent directory {agent_root.name!r} does not match manifest {name!r}"
                )
            if name in staged_packages:
                raise ManifestValidationError(f"duplicate Agent Package {name!r}")
            package = self._resolve_governance(package)
            staged_packages[name] = package
            loaded.append(package)
        self._packages = staged_packages
        return tuple(loaded)

    def get(self, name: str) -> AgentPackage:
        try:
            return self._packages[name]
        except KeyError as error:
            raise LookupError(f"unknown Agent Package {name!r}") from error

    def list(self) -> tuple[AgentPackage, ...]:
        return tuple(self._packages[name] for name in sorted(self._packages))

    def _resolve_governance(self, package: AgentPackage) -> AgentPackage:
        policy_hash = ""
        model_policy_hash = ""
        if self.policy_registry is not None:
            policy_hash = self.policy_registry.hash(package.manifest.policy_ref)
        if self.model_policy_registry is not None:
            model_policy = self.model_policy_registry.get(package.manifest.model_policy_ref)
            model_policy_hash = model_policy.sha256
            if model_policy.route != package.manifest.runtime.route:
                raise ManifestValidationError(
                    f"Agent route {package.manifest.runtime.route!r} conflicts with "
                    f"model policy route {model_policy.route!r}"
                )
        skills = self._resolve_skills(package)
        skill_set_hash = self._skill_set_hash(skills)
        execution_profiles = self._resolve_execution_profiles(package)
        self._validate_skill_execution_profiles(package, skills, execution_profiles)
        execution_profile_set_hash = self._execution_profile_set_hash(execution_profiles)
        if (
            package.snapshot.policy_hash == policy_hash
            and package.snapshot.model_policy_hash == model_policy_hash
            and package.snapshot.skill_set_hash == skill_set_hash
            and package.snapshot.execution_profile_set_hash == execution_profile_set_hash
        ):
            return package
        if (
            not policy_hash
            and not model_policy_hash
            and not skill_set_hash
            and not execution_profile_set_hash
        ):
            return package
        digest = hashlib.sha256(package.snapshot.package_hash.encode())
        digest.update(policy_hash.encode())
        digest.update(model_policy_hash.encode())
        digest.update(skill_set_hash.encode())
        digest.update(execution_profile_set_hash.encode())
        snapshot = replace(
            package.snapshot,
            package_hash=digest.hexdigest(),
            policy_hash=policy_hash,
            model_policy_hash=model_policy_hash,
            skill_set_hash=skill_set_hash,
            execution_profile_set_hash=execution_profile_set_hash,
        )
        return replace(
            package,
            snapshot=snapshot,
            skills=skills,
            execution_profiles=execution_profiles,
        )

    def _resolve_skills(self, package: AgentPackage):
        from types import MappingProxyType

        declaration = package.manifest.skills
        overlap = set(declaration.allow) & set(declaration.deny)
        if overlap:
            raise ManifestValidationError(
                f"Skills cannot be both allowed and denied: {', '.join(sorted(overlap))}"
            )
        active_refs = tuple(ref for ref in declaration.allow if ref not in declaration.deny)
        if not active_refs:
            return MappingProxyType({})
        if self.skill_registry is None:
            raise ManifestValidationError(
                "Agent declares Skills but no Skill registry is configured"
            )
        try:
            skills = {ref: self.skill_registry.get(ref) for ref in active_refs}
        except LookupError as error:
            raise ManifestValidationError(str(error)) from error
        self._validate_skill_capabilities(package, skills)
        return MappingProxyType(skills)

    @staticmethod
    def _validate_skill_capabilities(package: AgentPackage, skills) -> None:
        from fnmatch import fnmatch

        capabilities = package.manifest.capabilities
        for ref, skill in skills.items():
            missing = {
                name
                for name in skill.manifest.capabilities.required
                if not any(fnmatch(name, pattern) for pattern in capabilities.allow)
                or any(fnmatch(name, pattern) for pattern in capabilities.deny)
            }
            if missing:
                raise ManifestValidationError(
                    f"Agent {package.manifest.metadata.name!r} cannot grant Skill {ref!r} "
                    f"required capabilities: {', '.join(sorted(missing))}"
                )

    @staticmethod
    def _skill_set_hash(skills) -> str:
        if not skills:
            return ""
        digest = hashlib.sha256()
        for ref in sorted(skills):
            digest.update(ref.encode())
            digest.update(skills[ref].snapshot.package_hash.encode())
        return digest.hexdigest()

    def _resolve_execution_profiles(self, package: AgentPackage):
        from types import MappingProxyType

        declaration = package.manifest.execution_profiles
        overlap = set(declaration.allow) & set(declaration.deny)
        if overlap:
            raise ManifestValidationError(
                "Execution Profiles cannot be both allowed and denied: "
                f"{', '.join(sorted(overlap))}"
            )
        active_refs = tuple(ref for ref in declaration.allow if ref not in declaration.deny)
        if not active_refs:
            return MappingProxyType({})
        if self.execution_profile_registry is None:
            raise ManifestValidationError(
                "Agent declares Execution Profiles but no registry is configured"
            )
        try:
            profiles = {ref: self.execution_profile_registry.get(ref) for ref in active_refs}
        except LookupError as error:
            raise ManifestValidationError(str(error)) from error
        return MappingProxyType(profiles)

    @staticmethod
    def _validate_skill_execution_profiles(package, skills, profiles) -> None:
        available = set(profiles)
        for ref, skill in skills.items():
            missing = set(skill.manifest.execution_profiles) - available
            if missing:
                raise ManifestValidationError(
                    f"Agent {package.manifest.metadata.name!r} cannot grant Skill {ref!r} "
                    f"Execution Profiles: {', '.join(sorted(missing))}"
                )

    @staticmethod
    def _execution_profile_set_hash(profiles) -> str:
        if not profiles:
            return ""
        digest = hashlib.sha256()
        for ref in sorted(profiles):
            digest.update(ref.encode())
            digest.update(profiles[ref].sha256.encode())
        return digest.hexdigest()
