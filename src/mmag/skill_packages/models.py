"""Immutable value objects for Skill Package v1."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    version: str
    description: str

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class SkillActivation:
    intents: tuple[str, ...]
    keywords: tuple[str, ...]
    priority: int


@dataclass(frozen=True, slots=True)
class SkillCapabilities:
    required: tuple[str, ...]
    optional: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillResources:
    templates: tuple[str, ...]
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillDisclosure:
    max_resources: int
    max_resource_bytes: int
    max_total_bytes: int
    max_estimated_tokens: int


@dataclass(frozen=True, slots=True)
class SkillManifest:
    api_version: str
    kind: str
    metadata: SkillMetadata
    instruction_ref: str
    input_schema_ref: str
    output_schema_ref: str
    activation: SkillActivation
    capabilities: SkillCapabilities
    execution_profiles: tuple[str, ...]
    resources: SkillResources
    disclosure: SkillDisclosure


@dataclass(frozen=True, slots=True)
class SkillFileAsset:
    ref: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SkillSchemaAsset:
    ref: str
    schema: Mapping[str, Any]
    sha256: str
    version: str


@dataclass(frozen=True, slots=True)
class SkillEvalAsset:
    ref: str
    version: str
    cases: tuple[Mapping[str, Any], ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class SkillVersionSnapshot:
    skill_name: str
    skill_version: str
    package_hash: str
    instruction_hash: str
    input_schema_version: str
    output_schema_version: str
    eval_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "skill_package_hash": self.package_hash,
            "skill_instruction_hash": self.instruction_hash,
            "skill_input_schema_version": self.input_schema_version,
            "skill_output_schema_version": self.output_schema_version,
            "skill_eval_hash": self.eval_hash,
        }


@dataclass(frozen=True, slots=True)
class SkillPackage:
    root: Path
    manifest: SkillManifest
    instruction: SkillFileAsset
    schemas: Mapping[str, SkillSchemaAsset]
    resources: Mapping[str, SkillFileAsset]
    evals: Mapping[str, SkillEvalAsset]
    snapshot: SkillVersionSnapshot


def frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))
