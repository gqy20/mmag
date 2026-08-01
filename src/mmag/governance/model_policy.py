"""Strict model-routing policy registry."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

_FIELDS = frozenset({"id", "version", "route", "model_class", "max_output_tokens", "temperature"})


class ModelPolicyDocumentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    id: str
    version: str
    route: str
    model_class: str
    max_output_tokens: int
    temperature: float
    sha256: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


class ModelPolicyRegistry:
    def __init__(self) -> None:
        self._policies: dict[str, ModelPolicy] = {}

    def load_directory(self, root: Path) -> None:
        staged = dict(self._policies)
        for path in sorted(root.glob("*.yml")):
            policy = self._load(path)
            if policy.ref in staged:
                raise ModelPolicyDocumentError(f"duplicate model policy {policy.ref!r}")
            staged[policy.ref] = policy
        self._policies = staged

    def get(self, ref: str) -> ModelPolicy:
        try:
            return self._policies[ref]
        except KeyError as error:
            raise LookupError(f"unknown model policy {ref!r}") from error

    @staticmethod
    def _load(path: Path) -> ModelPolicy:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ModelPolicyDocumentError(f"invalid model policy YAML {path}: {error}") from error
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            keys = set(raw) if isinstance(raw, dict) else set()
            raise ModelPolicyDocumentError(
                f"model policy {path} has unknown={sorted(keys - _FIELDS)} "
                f"missing={sorted(_FIELDS - keys)}"
            )
        policy_id, version = raw["id"], raw["version"]
        if not isinstance(policy_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", policy_id):
            raise ModelPolicyDocumentError(f"model policy {path} has an invalid id")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ModelPolicyDocumentError(f"model policy {path} has an invalid version")
        if not all(isinstance(raw[key], str) and raw[key] for key in ("route", "model_class")):
            raise ModelPolicyDocumentError("route and model_class must be non-empty strings")
        if not isinstance(raw["max_output_tokens"], int) or raw["max_output_tokens"] < 1:
            raise ModelPolicyDocumentError("max_output_tokens must be a positive integer")
        temperature = raw["temperature"]
        if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
            raise ModelPolicyDocumentError("temperature must be between 0 and 2")
        return ModelPolicy(
            policy_id,
            version,
            raw["route"],
            raw["model_class"],
            raw["max_output_tokens"],
            float(temperature),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
