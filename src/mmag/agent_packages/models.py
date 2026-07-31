"""Immutable value objects for Agent Package v1."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class PackageMetadata:
    name: str
    version: str
    description: str


@dataclass(frozen=True, slots=True)
class PromptDeclaration:
    system_ref: str
    task_ref: str
    output_repair_ref: str | None
    required_variables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeDeclaration:
    route: str
    max_turns: int
    timeout_seconds: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    allow: tuple[str, ...]
    deny: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextDeclaration:
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BudgetDeclaration:
    max_model_calls: int
    max_tool_calls: int
    max_cost_usd: float


@dataclass(frozen=True, slots=True)
class ArtifactDeclaration:
    kind: str
    schema_ref: str


@dataclass(frozen=True, slots=True)
class AgentManifest:
    api_version: str
    kind: str
    metadata: PackageMetadata
    accepted_intents: tuple[str, ...]
    prompt: PromptDeclaration
    input_schema_ref: str
    result_schema_ref: str
    runtime: RuntimeDeclaration
    capabilities: CapabilityDeclaration
    context: ContextDeclaration
    policy_ref: str
    model_policy_ref: str
    budget: BudgetDeclaration
    artifacts: tuple[ArtifactDeclaration, ...]


@dataclass(frozen=True, slots=True)
class PromptAsset:
    ref: str
    content: str
    sha256: str
    variables: frozenset[str]


@dataclass(frozen=True, slots=True)
class SchemaAsset:
    ref: str
    schema: Mapping[str, Any]
    sha256: str
    version: str


@dataclass(frozen=True, slots=True)
class PackageVersionSnapshot:
    agent_name: str
    agent_spec_version: str
    package_hash: str
    prompt_id: str
    prompt_version: str
    prompt_hash: str
    input_schema_version: str
    output_schema_version: str
    policy_version: str
    model_policy_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "agent_name": self.agent_name,
            "agent_spec_version": self.agent_spec_version,
            "package_hash": self.package_hash,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "policy_version": self.policy_version,
            "model_policy_version": self.model_policy_version,
        }


@dataclass(frozen=True, slots=True)
class AgentPackage:
    root: Path
    manifest: AgentManifest
    prompts: Mapping[str, PromptAsset]
    schemas: Mapping[str, SchemaAsset]
    snapshot: PackageVersionSnapshot
