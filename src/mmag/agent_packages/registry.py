"""Atomic loading of flat, self-versioned Agent Packages."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from .errors import ManifestValidationError
from .loader import AgentPackageLoader

if TYPE_CHECKING:
    from pathlib import Path

    from ..governance import ModelPolicyRegistry, PolicyRegistry
    from .models import AgentPackage


class AgentPackageRegistry:
    def __init__(
        self,
        loader: AgentPackageLoader | None = None,
        *,
        policy_registry: PolicyRegistry | None = None,
        model_policy_registry: ModelPolicyRegistry | None = None,
    ) -> None:
        self.loader = loader or AgentPackageLoader()
        self.policy_registry = policy_registry
        self.model_policy_registry = model_policy_registry
        self._packages: dict[str, AgentPackage] = {}

    def load_directory(self, root: Path) -> tuple[AgentPackage, ...]:
        staged_packages: dict[str, AgentPackage] = {}
        loaded: list[AgentPackage] = []
        for agent_root in sorted(path for path in root.iterdir() if path.is_dir()):
            package = self._resolve_governance(self.loader.load(agent_root))
            name = package.manifest.metadata.name
            if name != agent_root.name:
                raise ManifestValidationError(
                    f"Agent directory {agent_root.name!r} does not match manifest {name!r}"
                )
            if name in staged_packages:
                raise ManifestValidationError(f"duplicate Agent Package {name!r}")
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
        if (
            package.snapshot.policy_hash == policy_hash
            and package.snapshot.model_policy_hash == model_policy_hash
        ):
            return package
        if not policy_hash and not model_policy_hash:
            return package
        digest = hashlib.sha256(package.snapshot.package_hash.encode())
        digest.update(policy_hash.encode())
        digest.update(model_policy_hash.encode())
        snapshot = replace(
            package.snapshot,
            package_hash=digest.hexdigest(),
            policy_hash=policy_hash,
            model_policy_hash=model_policy_hash,
        )
        return replace(package, snapshot=snapshot)
