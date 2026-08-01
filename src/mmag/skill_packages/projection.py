"""Project validated MMAG Skills into Deep Agents' ephemeral filesystem."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import yaml

from .loader import load_skill_instructions, resolve_skill_ref

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..agent_packages import AgentPackage
    from ..agent_system import AgentRequest


def project_skill_files(
    package: AgentPackage,
    request: AgentRequest,
) -> Mapping[str, Mapping[str, str]]:
    """Return immutable-input file records for the one governed active Skill."""
    if request.skill is None:
        return {}
    skill = package.skills[request.skill.ref]
    metadata = skill.manifest.metadata
    frontmatter = yaml.safe_dump(
        {
            "name": metadata.name,
            "description": metadata.description,
            "metadata": {
                "version": metadata.version,
                "package_hash": skill.snapshot.package_hash,
            },
        },
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    files: dict[str, Mapping[str, str]] = {
        f"/skills/{metadata.name}/SKILL.md": {
            "content": f"---\n{frontmatter}\n---\n\n{load_skill_instructions(skill)}",
            "encoding": "utf-8",
        }
    }
    for ref, asset in skill.resources.items():
        raw = resolve_skill_ref(skill.root, ref).read_bytes()
        if hashlib.sha256(raw).hexdigest() != asset.sha256:
            raise ValueError(f"Skill resource changed after registration: {metadata.ref}/{ref}")
        files[f"/skills/{metadata.name}/{ref}"] = {
            "content": raw.decode("utf-8"),
            "encoding": "utf-8",
        }
    return files
