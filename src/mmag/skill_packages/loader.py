"""Strict loader for flat, self-versioned Skill Package directories."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .errors import SkillManifestError, SkillReferenceError
from .models import (
    SkillActivation,
    SkillCapabilities,
    SkillDisclosure,
    SkillEvalAsset,
    SkillFileAsset,
    SkillManifest,
    SkillMetadata,
    SkillPackage,
    SkillResources,
    SkillSchemaAsset,
    SkillVersionSnapshot,
    frozen_mapping,
)


def _manifest_schema() -> dict[str, Any]:
    resource = files("mmag.skill_packages").joinpath("schemas/skill-manifest-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def resolve_skill_ref(root: Path, ref: str) -> Path:
    candidate = (root / ref).resolve()
    if Path(ref).is_absolute() or not candidate.is_relative_to(root.resolve()):
        raise SkillReferenceError(f"Skill reference escapes its Package: {ref!r}")
    if not candidate.is_file():
        raise SkillReferenceError(f"Skill reference does not exist: {ref!r}")
    return candidate


def _file_asset(root: Path, ref: str) -> SkillFileAsset:
    content = resolve_skill_ref(root, ref).read_bytes()
    return SkillFileAsset(ref, hashlib.sha256(content).hexdigest(), len(content))


def _schema_asset(root: Path, ref: str) -> SkillSchemaAsset:
    raw = resolve_skill_ref(root, ref).read_bytes()
    try:
        schema = json.loads(raw)
        Draft202012Validator.check_schema(schema)
    except (json.JSONDecodeError, SchemaError) as error:
        raise SkillReferenceError(f"invalid Skill JSON Schema {ref!r}: {error}") from error
    version = schema.get("x-version")
    if not isinstance(version, str) or not version:
        raise SkillReferenceError(f"Skill JSON Schema {ref!r} must declare x-version")
    return SkillSchemaAsset(
        ref,
        frozen_mapping(schema),
        hashlib.sha256(raw).hexdigest(),
        version,
    )


class SkillPackageLoader:
    """Validate all Skill-owned resources without executing any of them."""

    def __init__(self) -> None:
        self._validator = Draft202012Validator(_manifest_schema())

    def load(self, package_root: Path) -> SkillPackage:
        package_root = package_root.resolve()
        raw = self._load_manifest(package_root / "skill.yml")
        manifest = self._parse(raw)
        overlap = set(manifest.capabilities.required) & set(manifest.capabilities.optional)
        if overlap:
            raise SkillManifestError(
                f"capabilities cannot be both required and optional: {', '.join(sorted(overlap))}"
            )
        self._validate_resources(manifest)

        instruction = _file_asset(package_root, manifest.instruction_ref)
        schemas = {
            ref: _schema_asset(package_root, ref)
            for ref in (manifest.input_schema_ref, manifest.output_schema_ref)
        }
        resource_refs = (
            *manifest.resources.templates,
            *manifest.resources.references,
        )
        resources = {ref: _file_asset(package_root, ref) for ref in resource_refs}
        self._validate_resource_assets(manifest, resources, package_root)
        evals = self._load_evals(package_root)
        package_hash = self._package_hash(raw, instruction, schemas, resources, evals)
        eval_hash = hashlib.sha256(
            "".join(evals[ref].sha256 for ref in sorted(evals)).encode()
        ).hexdigest()
        snapshot = SkillVersionSnapshot(
            manifest.metadata.name,
            manifest.metadata.version,
            package_hash,
            instruction.sha256,
            schemas[manifest.input_schema_ref].version,
            schemas[manifest.output_schema_ref].version,
            eval_hash,
        )
        return SkillPackage(
            package_root,
            manifest,
            instruction,
            MappingProxyType(schemas),
            MappingProxyType(resources),
            MappingProxyType(evals),
            snapshot,
        )

    @staticmethod
    def _validate_resources(manifest: SkillManifest) -> None:
        resources = manifest.resources
        refs = (*resources.templates, *resources.references)
        if len(refs) != len(set(refs)):
            raise SkillManifestError("a Skill resource cannot have multiple resource kinds")
        disclosure = manifest.disclosure
        if disclosure.max_resource_bytes > disclosure.max_total_bytes:
            raise SkillManifestError("max_resource_bytes cannot exceed max_total_bytes")

    @staticmethod
    def _validate_resource_assets(
        manifest: SkillManifest,
        assets: dict[str, SkillFileAsset],
        package_root: Path,
    ) -> None:
        refs = (*manifest.resources.templates, *manifest.resources.references)
        oversized = [
            ref for ref in refs if assets[ref].size_bytes > manifest.disclosure.max_resource_bytes
        ]
        if oversized:
            raise SkillManifestError(
                f"Skill resources exceed max_resource_bytes: {', '.join(sorted(oversized))}"
            )
        for ref in refs:
            try:
                resolve_skill_ref(package_root, ref).read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise SkillManifestError(
                    f"disclosable Skill resource {ref!r} is not UTF-8"
                ) from error

    def _load_manifest(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise SkillManifestError(f"missing Skill manifest: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise SkillManifestError(f"invalid Skill manifest YAML: {error}") from error
        if not isinstance(raw, dict):
            raise SkillManifestError("Skill manifest must be a mapping")
        try:
            self._validator.validate(raw)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise SkillManifestError(
                f"Skill manifest failed at {location}: {error.message}"
            ) from error
        return raw

    @staticmethod
    def _parse(raw: dict[str, Any]) -> SkillManifest:
        metadata = raw["metadata"]
        spec = raw["spec"]
        activation = spec["activation"]
        resources = spec["resources"]
        return SkillManifest(
            raw["api_version"],
            raw["kind"],
            SkillMetadata(metadata["name"], metadata["version"], metadata["description"]),
            spec["instruction_ref"],
            spec["input_schema_ref"],
            spec["output_schema_ref"],
            SkillActivation(
                tuple(activation["intents"]),
                tuple(activation["keywords"]),
                activation["priority"],
            ),
            SkillCapabilities(
                tuple(spec["required_capabilities"]),
                tuple(spec["optional_capabilities"]),
            ),
            tuple(spec.get("execution_profiles", ())),
            SkillResources(
                tuple(resources["templates"]),
                tuple(resources["references"]),
            ),
            SkillDisclosure(
                spec["disclosure"]["max_resources"],
                spec["disclosure"]["max_resource_bytes"],
                spec["disclosure"]["max_total_bytes"],
                spec["disclosure"]["max_estimated_tokens"],
            ),
        )

    @staticmethod
    def _load_evals(package_root: Path) -> dict[str, SkillEvalAsset]:
        assets: dict[str, SkillEvalAsset] = {}
        eval_path = package_root / "evals.yml"
        for path in (eval_path,) if eval_path.is_file() else ():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as error:
                raise SkillManifestError(f"invalid Skill eval YAML {path}: {error}") from error
            if not isinstance(raw, dict) or set(raw) != {"version", "cases"}:
                raise SkillManifestError(f"Skill eval {path} must contain version and cases")
            if not isinstance(raw["version"], int) or raw["version"] < 1:
                raise SkillManifestError(f"Skill eval {path} has an invalid version")
            if not isinstance(raw["cases"], list) or not raw["cases"]:
                raise SkillManifestError(f"Skill eval {path} must contain cases")
            cases = SkillPackageLoader._validate_eval_cases(path, raw["cases"])
            ref = str(path.relative_to(package_root))
            assets[ref] = SkillEvalAsset(
                ref,
                str(raw["version"]),
                tuple(MappingProxyType(case) for case in cases),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return assets

    @staticmethod
    def _validate_eval_cases(path: Path, raw_cases: list[Any]) -> list[dict[str, Any]]:
        case_ids: set[str] = set()
        cases: list[dict[str, Any]] = []
        for case in raw_cases:
            if not isinstance(case, dict):
                raise SkillManifestError(f"Skill eval {path} case must be a mapping")
            if not isinstance(case.get("id"), str) or not case["id"]:
                raise SkillManifestError(f"Skill eval {path} case is missing id")
            if not isinstance(case.get("input"), dict):
                raise SkillManifestError(f"Skill eval {path} case {case['id']!r} needs input")
            if ("expect" in case) == ("expect_error" in case):
                raise SkillManifestError(
                    f"Skill eval {path} case {case['id']!r} needs exactly one expectation"
                )
            if case["id"] in case_ids:
                raise SkillManifestError(f"Skill eval {path} has duplicate case {case['id']!r}")
            case_ids.add(case["id"])
            cases.append(dict(case))
        return cases

    @staticmethod
    def _package_hash(
        raw: dict[str, Any],
        instruction: SkillFileAsset,
        schemas: dict[str, SkillSchemaAsset],
        resources: dict[str, SkillFileAsset],
        evals: dict[str, SkillEvalAsset],
    ) -> str:
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode())
        digest.update(instruction.sha256.encode())
        for assets in (schemas, resources, evals):
            for ref in sorted(assets):
                digest.update(ref.encode())
                digest.update(assets[ref].sha256.encode())
        return digest.hexdigest()


def load_skill_instructions(package: SkillPackage) -> str:
    """Materialize instructions only after selection and verify the registered hash."""
    raw = resolve_skill_ref(package.root, package.instruction.ref).read_bytes()
    if hashlib.sha256(raw).hexdigest() != package.instruction.sha256:
        raise SkillReferenceError(f"Skill instruction changed after registration: {package.root}")
    return raw.decode("utf-8")
