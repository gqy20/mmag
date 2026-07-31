"""Strict loader for versioned Agent Package directories."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .assets import PromptRegistry, SchemaRegistry
from .errors import ManifestValidationError
from .models import (
    AgentManifest,
    AgentPackage,
    ArtifactDeclaration,
    BudgetDeclaration,
    CapabilityDeclaration,
    ContextDeclaration,
    PackageMetadata,
    PackageVersionSnapshot,
    PromptDeclaration,
    RuntimeDeclaration,
)


def _load_manifest_schema() -> dict[str, Any]:
    resource = files("mmag.agent_packages").joinpath("schemas/agent-manifest-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _version_from_ref(ref: str) -> str:
    if "@" in ref:
        return ref.rsplit("@", 1)[1]
    for part in Path(ref).parts:
        if part.startswith("v") and part[1:].isdigit():
            return part
    return "unversioned"


class AgentPackageLoader:
    """Load all referenced assets before publishing one immutable package."""

    def __init__(self) -> None:
        self._validator = Draft202012Validator(_load_manifest_schema())

    def load(self, package_root: Path) -> AgentPackage:
        package_root = package_root.resolve()
        manifest_path = package_root / "agent.yml"
        if not manifest_path.is_file():
            raise ManifestValidationError(f"missing Agent manifest: {manifest_path}")
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ManifestValidationError(f"invalid Agent manifest YAML: {error}") from error
        if not isinstance(raw, dict):
            raise ManifestValidationError("Agent manifest must be a mapping")
        try:
            self._validator.validate(raw)
        except ValidationError as error:
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ManifestValidationError(f"manifest failed at {path}: {error.message}") from error

        manifest = self._parse(raw)
        if set(manifest.capabilities.allow).intersection(manifest.capabilities.deny):
            raise ManifestValidationError("a capability cannot be both allowed and denied")

        prompt_registry = PromptRegistry(package_root, manifest.prompt.required_variables)
        prompt_refs = [manifest.prompt.system_ref, manifest.prompt.task_ref]
        if manifest.prompt.output_repair_ref:
            prompt_refs.append(manifest.prompt.output_repair_ref)
        for ref in prompt_refs:
            prompt_registry.load(ref)
        prompt_registry.validate_declaration()

        schema_registry = SchemaRegistry(package_root)
        schema_refs = [manifest.input_schema_ref, manifest.result_schema_ref]
        schema_refs.extend(artifact.schema_ref for artifact in manifest.artifacts)
        for ref in dict.fromkeys(schema_refs):
            schema_registry.load(ref)

        prompts = prompt_registry.assets
        schemas = schema_registry.assets
        package_hash = self._package_hash(raw, prompts, schemas)
        prompt_hash = hashlib.sha256(
            "".join(prompts[ref].sha256 for ref in prompt_refs).encode()
        ).hexdigest()
        snapshot = PackageVersionSnapshot(
            agent_name=manifest.metadata.name,
            agent_spec_version=manifest.metadata.version,
            package_hash=package_hash,
            prompt_id=f"{manifest.metadata.name}:system-task",
            prompt_version=_version_from_ref(manifest.prompt.system_ref),
            prompt_hash=prompt_hash,
            input_schema_version=schemas[manifest.input_schema_ref].version,
            output_schema_version=schemas[manifest.result_schema_ref].version,
            policy_version=_version_from_ref(manifest.policy_ref),
            model_policy_version=_version_from_ref(manifest.model_policy_ref),
        )
        return AgentPackage(
            package_root,
            manifest,
            MappingProxyType(prompts),
            MappingProxyType(schemas),
            snapshot,
        )

    @staticmethod
    def _package_hash(raw: dict, prompts: dict, schemas: dict) -> str:
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode())
        for ref in sorted(prompts):
            digest.update(ref.encode())
            digest.update(prompts[ref].sha256.encode())
        for ref in sorted(schemas):
            digest.update(ref.encode())
            digest.update(schemas[ref].sha256.encode())
        return digest.hexdigest()

    @staticmethod
    def _parse(raw: dict) -> AgentManifest:
        metadata = raw["metadata"]
        spec = raw["spec"]
        prompt = spec["prompt"]
        runtime = spec["runtime"]
        capabilities = spec["capabilities"]
        context = spec["context"]
        budget = spec["budget"]
        return AgentManifest(
            raw["api_version"],
            raw["kind"],
            PackageMetadata(metadata["name"], metadata["version"], metadata["description"]),
            tuple(spec["accepted_intents"]),
            PromptDeclaration(
                prompt["system_ref"],
                prompt["task_ref"],
                prompt.get("output_repair_ref"),
                tuple(prompt["required_variables"]),
            ),
            spec["input_schema_ref"],
            spec["result_schema_ref"],
            RuntimeDeclaration(
                runtime["route"],
                runtime["max_turns"],
                runtime["timeout_seconds"],
                runtime["retry"]["max_attempts"],
            ),
            CapabilityDeclaration(tuple(capabilities["allow"]), tuple(capabilities["deny"])),
            ContextDeclaration(tuple(context["read_scopes"]), tuple(context["write_scopes"])),
            spec["policy_ref"],
            spec["model_policy_ref"],
            BudgetDeclaration(
                budget["max_model_calls"], budget["max_tool_calls"], budget["max_cost_usd"]
            ),
            tuple(
                ArtifactDeclaration(item["kind"], item["schema_ref"])
                for item in spec["artifacts"]["produces"]
            ),
        )
