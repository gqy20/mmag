"""Atomic registry for flat, self-versioned Skill Packages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import SkillManifestError
from .loader import SkillPackageLoader

if TYPE_CHECKING:
    from pathlib import Path

    from .models import SkillPackage


class SkillPackageRegistry:
    def __init__(self, loader: SkillPackageLoader | None = None) -> None:
        self.loader = loader or SkillPackageLoader()
        self._packages: dict[str, SkillPackage] = {}

    def load_directory(self, root: Path) -> tuple[SkillPackage, ...]:
        staged: dict[str, SkillPackage] = {}
        names: set[str] = set()
        for skill_root in sorted(path for path in root.iterdir() if path.is_dir()):
            package = self.loader.load(skill_root)
            metadata = package.manifest.metadata
            if metadata.name != skill_root.name:
                raise SkillManifestError(
                    f"Skill directory {skill_root.name!r} does not match manifest {metadata.name!r}"
                )
            if metadata.name in names or metadata.ref in staged:
                raise SkillManifestError(f"duplicate Skill Package {metadata.ref!r}")
            names.add(metadata.name)
            staged[metadata.ref] = package
        self._packages = staged
        return self.list()

    def get(self, ref: str) -> SkillPackage:
        try:
            return self._packages[ref]
        except KeyError as error:
            raise LookupError(f"unknown Skill Package {ref!r}") from error

    def list(self) -> tuple[SkillPackage, ...]:
        return tuple(self._packages[ref] for ref in sorted(self._packages))
