"""Strict loader for top-level system evaluation assets."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .models import (
    EvaluationActorRef,
    EvaluationProfile,
    EvaluationScenario,
    EvaluationSuite,
)


class EvaluationAssetError(ValueError):
    pass


class EvaluationAssetLoader:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._schemas = {
            kind: self._load_schema(self.root / "schemas" / f"{kind}.schema.json")
            for kind in ("scenario", "suite", "profile", "result")
        }

    def load_scenario(self, ref: str | Path) -> EvaluationScenario:
        path = self._resolve(ref)
        raw, digest = self._load_yaml(path, "scenario")
        metadata = raw["metadata"]
        spec = raw["spec"]
        return EvaluationScenario(
            id=metadata["id"],
            version=metadata["version"],
            actor=spec["actor"],
            message=spec["input"]["message"],
            expected=spec["expected"],
            thresholds=spec.get("thresholds", {}),
            tags=tuple(metadata.get("tags", ())),
            sha256=digest,
        )

    def load_suite(self, ref: str | Path) -> EvaluationSuite:
        path = self._resolve(ref)
        raw, digest = self._load_yaml(path, "suite")
        metadata = raw["metadata"]
        spec = raw["spec"]
        return EvaluationSuite(
            id=metadata["id"],
            version=metadata["version"],
            case_refs=tuple(spec["cases"]),
            repetitions=int(spec.get("repetitions", 1)),
            pass_policy=spec.get("pass_policy", {}),
            sha256=digest,
        )

    def load_profile(self, ref: str | Path) -> EvaluationProfile:
        path = self._resolve(ref)
        raw, digest = self._load_yaml(path, "profile")
        metadata = raw["metadata"]
        spec = raw["spec"]
        actors = {
            name: EvaluationActorRef(item["username_env"], item["password_env"])
            for name, item in spec["actors"].items()
        }
        return EvaluationProfile(
            id=metadata["id"],
            version=metadata["version"],
            driver=spec["driver"],
            base_url_env=spec["base_url_env"],
            channel_id_env=spec["channel_id_env"],
            actors=actors,
            bot_username_env=spec.get("bot_username_env", ""),
            enabled_env=spec.get("enabled_env", "MMAG_E2E_ENABLED"),
            control_plane_db_env=spec.get("control_plane_db_env", ""),
            timeout_seconds=float(spec.get("timeout_seconds", 120)),
            poll_interval_seconds=float(spec.get("poll_interval_seconds", 1)),
            sha256=digest,
        )

    def load_suite_cases(self, suite: EvaluationSuite) -> tuple[EvaluationScenario, ...]:
        scenarios = tuple(self.load_scenario(ref) for ref in suite.case_refs)
        ids = [scenario.id for scenario in scenarios]
        if len(ids) != len(set(ids)):
            raise EvaluationAssetError(f"suite {suite.id!r} contains duplicate case IDs")
        return scenarios

    def validate_tree(self) -> tuple[int, int, int]:
        profiles = tuple(sorted((self.root / "profiles").glob("*.yml")))
        suites = tuple(sorted((self.root / "suites").glob("*.yml")))
        cases = tuple(sorted((self.root / "cases").rglob("*.yml")))
        for path in profiles:
            self.load_profile(path)
        for path in cases:
            self.load_scenario(path)
        for path in suites:
            self.load_suite_cases(self.load_suite(path))
        return len(profiles), len(suites), len(cases)

    def validate_result(self, value: Any) -> None:
        try:
            Draft202012Validator(self._schemas["result"]).validate(value)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise EvaluationAssetError(
                f"evaluation result failed at {location}: {error.message}"
            ) from error

    def _resolve(self, ref: str | Path) -> Path:
        path = Path(ref)
        resolved = path.resolve() if path.is_absolute() else (self.root / path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise EvaluationAssetError(f"evaluation asset escapes root: {ref}") from error
        if not resolved.is_file():
            raise EvaluationAssetError(f"evaluation asset does not exist: {ref}")
        return resolved

    def _load_yaml(self, path: Path, kind: str) -> tuple[dict[str, Any], str]:
        content = path.read_bytes()
        try:
            raw = yaml.safe_load(content)
        except yaml.YAMLError as error:
            raise EvaluationAssetError(f"invalid YAML in {path}: {error}") from error
        if not isinstance(raw, dict):
            raise EvaluationAssetError(f"evaluation asset {path} must be an object")
        try:
            Draft202012Validator(self._schemas[kind]).validate(raw)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise EvaluationAssetError(
                f"{kind} asset {path} failed at {location}: {error.message}"
            ) from error
        return raw, hashlib.sha256(content).hexdigest()

    @staticmethod
    def _load_schema(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise EvaluationAssetError(f"evaluation schema does not exist: {path}")
        try:
            schema = yaml.safe_load(path.read_bytes())
            Draft202012Validator.check_schema(schema)
        except (yaml.YAMLError, TypeError) as error:
            raise EvaluationAssetError(f"invalid evaluation schema {path}: {error}") from error
        if not isinstance(schema, dict):
            raise EvaluationAssetError(f"evaluation schema {path} must be an object")
        return schema
