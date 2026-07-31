"""Atomic, version-aware publication of validated Agent Packages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .loader import AgentPackageLoader

if TYPE_CHECKING:
    from pathlib import Path

    from .models import AgentPackage


class AgentPackageRegistry:
    def __init__(self, loader: AgentPackageLoader | None = None):
        self.loader = loader or AgentPackageLoader()
        self._packages: dict[tuple[str, str], AgentPackage] = {}
        self._active: dict[str, str] = {}

    def publish(self, package: AgentPackage, *, activate: bool = True) -> None:
        key = (package.manifest.metadata.name, package.manifest.metadata.version)
        existing = self._packages.get(key)
        if existing and existing.snapshot.package_hash != package.snapshot.package_hash:
            raise ValueError(f"published Agent Package {key!r} is immutable")
        self._packages[key] = package
        if activate:
            self._active[key[0]] = key[1]

    def load_directory(self, root: Path) -> tuple[AgentPackage, ...]:
        candidates = tuple(sorted(path.parent for path in root.glob("*/agent.yml")))
        loaded = tuple(self.loader.load(path) for path in candidates)
        staged_packages = dict(self._packages)
        staged_active = dict(self._active)
        for package in loaded:
            key = (package.manifest.metadata.name, package.manifest.metadata.version)
            existing = staged_packages.get(key)
            if existing and existing.snapshot.package_hash != package.snapshot.package_hash:
                raise ValueError(f"published Agent Package {key!r} is immutable")
            staged_packages[key] = package
            staged_active[key[0]] = key[1]
        self._packages = staged_packages
        self._active = staged_active
        return loaded

    def get(self, name: str, version: str | None = None) -> AgentPackage:
        selected_version = version or self._active.get(name)
        if selected_version is None:
            raise LookupError(f"unknown Agent Package {name!r}")
        try:
            return self._packages[(name, selected_version)]
        except KeyError as error:
            raise LookupError(f"unknown Agent Package {name!r}@{selected_version}") from error

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(
            sorted(version for package_name, version in self._packages if package_name == name)
        )
