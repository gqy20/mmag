"""Strict loader for flat, self-versioned Agent Package directories."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

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
    EvalAsset,
    ExecutionDeclaration,
    ExecutionProfileDeclaration,
    PackageMetadata,
    PackageVersionSnapshot,
    PromptDeclaration,
    RoutingDeclaration,
    RuntimeDeclaration,
    SkillDeclaration,
)

if TYPE_CHECKING:
    from pathlib import Path


def _load_manifest_schema() -> dict[str, Any]:
    resource = files("mmag.agent_packages").joinpath("schemas/agent-manifest-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _version_from_ref(ref: str) -> str:
    return ref.rsplit("@", 1)[1]


class AgentPackageLoader:
    """Load every referenced asset before registering one Package."""

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
        evals = self._load_evals(package_root)
        package_hash = self._package_hash(raw, prompts, schemas, evals)
        prompt_hash = hashlib.sha256(
            "".join(prompts[ref].sha256 for ref in prompt_refs).encode()
        ).hexdigest()
        snapshot = PackageVersionSnapshot(
            agent_name=manifest.metadata.name,
            agent_spec_version=manifest.metadata.version,
            package_hash=package_hash,
            prompt_id=f"{manifest.metadata.name}:system-task",
            prompt_version=manifest.metadata.version,
            prompt_hash=prompt_hash,
            input_schema_version=schemas[manifest.input_schema_ref].version,
            output_schema_version=schemas[manifest.result_schema_ref].version,
            policy_version=_version_from_ref(manifest.policy_ref),
            model_policy_version=_version_from_ref(manifest.model_policy_ref),
            eval_hash=hashlib.sha256(
                "".join(evals[ref].sha256 for ref in sorted(evals)).encode()
            ).hexdigest(),
        )
        return AgentPackage(
            package_root,
            manifest,
            MappingProxyType(prompts),
            MappingProxyType(schemas),
            snapshot,
            MappingProxyType(evals),
        )

    @staticmethod
    def _package_hash(raw: dict, prompts: dict, schemas: dict, evals: dict) -> str:
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode())
        for ref in sorted(prompts):
            digest.update(ref.encode())
            digest.update(prompts[ref].sha256.encode())
        for ref in sorted(schemas):
            digest.update(ref.encode())
            digest.update(schemas[ref].sha256.encode())
        for ref in sorted(evals):
            digest.update(ref.encode())
            digest.update(evals[ref].sha256.encode())
        return digest.hexdigest()

    @staticmethod
    def _load_evals(package_root: Path) -> dict[str, EvalAsset]:
        assets: dict[str, EvalAsset] = {}
        for path in sorted((package_root / "evals").glob("*.yml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as error:
                raise ManifestValidationError(f"invalid eval YAML {path}: {error}") from error
            if not isinstance(raw, dict) or set(raw) != {"version", "cases"}:
                raise ManifestValidationError(f"eval {path} must contain only version and cases")
            if not isinstance(raw["version"], int) or raw["version"] < 1:
                raise ManifestValidationError(f"eval {path} has an invalid version")
            if not isinstance(raw["cases"], list) or not raw["cases"]:
                raise ManifestValidationError(f"eval {path} must contain cases")
            case_ids: set[str] = set()
            cases: list[dict] = []
            for case in raw["cases"]:
                if not isinstance(case, dict):
                    raise ManifestValidationError(f"eval {path} case must be a mapping")
                required = {"id", "intent", "goal"}
                if not required <= case.keys() or not all(
                    isinstance(case[key], str) and case[key] for key in required
                ):
                    raise ManifestValidationError(f"eval {path} case is missing id/intent/goal")
                if case["id"] in case_ids:
                    raise ManifestValidationError(f"eval {path} has duplicate case {case['id']!r}")
                if ("expect" in case) == ("expect_error" in case):
                    raise ManifestValidationError(
                        f"eval {path} case {case['id']!r} needs exactly one expectation"
                    )
                case_ids.add(case["id"])
                cases.append(case)
            ref = str(path.relative_to(package_root))
            assets[ref] = EvalAsset(
                ref,
                str(raw["version"]),
                tuple(MappingProxyType(dict(case)) for case in cases),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        if not assets:
            raise ManifestValidationError(f"Agent Package {package_root} has no eval suite")
        return assets

    @staticmethod
    def _parse(raw: dict) -> AgentManifest:
        metadata = raw["metadata"]
        spec = raw["spec"]
        execution = spec["execution"]
        routing = spec["routing"]
        prompt = spec["prompt"]
        runtime = spec["runtime"]
        capabilities = spec["capabilities"]
        context = spec["context"]
        budget = spec["budget"]
        return AgentManifest(
            raw["api_version"],
            raw["kind"],
            PackageMetadata(metadata["name"], metadata["version"], metadata["description"]),
            ExecutionDeclaration(
                execution["kind"],
                execution["provider"],
                execution.get("capability"),
                execution.get("source_argument"),
            ),
            RoutingDeclaration(
                routing["default"],
                routing["priority"],
                tuple(routing["keywords"]),
                routing["requires_url"],
                tuple(routing["scopes"]),
            ),
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
            SkillDeclaration(tuple(spec["skills"]["allow"]), tuple(spec["skills"]["deny"])),
            ExecutionProfileDeclaration(
                tuple(spec.get("execution_profiles", {}).get("allow", ())),
                tuple(spec.get("execution_profiles", {}).get("deny", ())),
            ),
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
