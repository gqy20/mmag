"""Safe loading and strict rendering of package-owned prompt and schema assets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from string import Formatter
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .errors import PackageReferenceError, PromptContractError, SchemaContractError
from .models import PromptAsset, SchemaAsset, frozen_mapping


def resolve_package_ref(root: Path, ref: str) -> Path:
    candidate = (root / ref).resolve()
    resolved_root = root.resolve()
    if Path(ref).is_absolute() or not candidate.is_relative_to(resolved_root):
        raise PackageReferenceError(f"package reference escapes its root: {ref!r}")
    if not candidate.is_file():
        raise PackageReferenceError(f"package reference does not exist: {ref!r}")
    return candidate


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def render_prompt(asset: PromptAsset, variables: dict[str, Any]) -> str:
    """Strictly render one immutable package prompt asset."""
    missing = asset.variables - variables.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise PromptContractError(f"prompt {asset.ref!r} is missing variables: {names}")
    try:
        return asset.content.format_map(variables)
    except (KeyError, ValueError) as error:
        raise PromptContractError(f"prompt {asset.ref!r} could not be rendered: {error}") from error


class PromptRegistry:
    def __init__(self, root: Path, declared_variables: tuple[str, ...]):
        self.root = root
        self.declared_variables = frozenset(declared_variables)
        self._assets: dict[str, PromptAsset] = {}

    def load(self, ref: str) -> PromptAsset:
        path = resolve_package_ref(self.root, ref)
        raw = path.read_bytes()
        content = raw.decode("utf-8")
        variables = frozenset(
            field_name
            for _, field_name, _, _ in Formatter().parse(content)
            if field_name is not None
        )
        undeclared = variables - self.declared_variables
        if undeclared:
            names = ", ".join(sorted(undeclared))
            raise PromptContractError(f"prompt {ref!r} uses undeclared variables: {names}")
        asset = PromptAsset(ref, content, _sha256(raw), variables)
        self._assets[ref] = asset
        return asset

    def validate_declaration(self) -> None:
        used = frozenset().union(*(asset.variables for asset in self._assets.values()))
        unused = self.declared_variables - used
        if unused:
            names = ", ".join(sorted(unused))
            raise PromptContractError(f"required prompt variables are not used: {names}")

    def render(self, ref: str, variables: dict[str, Any]) -> str:
        return render_prompt(self._assets[ref], variables)

    @property
    def assets(self) -> dict[str, PromptAsset]:
        return dict(self._assets)


class SchemaRegistry:
    def __init__(self, root: Path):
        self.root = root
        self._assets: dict[str, SchemaAsset] = {}

    def load(self, ref: str) -> SchemaAsset:
        path = resolve_package_ref(self.root, ref)
        raw = path.read_bytes()
        try:
            schema = json.loads(raw)
            Draft202012Validator.check_schema(schema)
        except (json.JSONDecodeError, SchemaError) as error:
            raise PackageReferenceError(f"invalid JSON Schema {ref!r}: {error}") from error
        version = schema.get("x-version")
        if not isinstance(version, str) or not version:
            raise PackageReferenceError(f"JSON Schema {ref!r} must declare x-version")
        asset = SchemaAsset(ref, frozen_mapping(schema), _sha256(raw), version)
        self._assets[ref] = asset
        return asset

    def validate(self, ref: str, value: Any, *, direction: str) -> None:
        asset = self._assets[ref]
        try:
            Draft202012Validator(asset.schema).validate(value)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise SchemaContractError(
                f"{direction} contract failed at {location}: {error.message}",
                direction=direction,
            ) from error

    @property
    def assets(self) -> dict[str, SchemaAsset]:
        return dict(self._assets)
