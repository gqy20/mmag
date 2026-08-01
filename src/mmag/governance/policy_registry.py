"""Strict loading and version lookup for policy-as-code documents."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

import yaml

from .policy import PolicyEffect, PolicyEngine, PolicyRule

if TYPE_CHECKING:
    from pathlib import Path

_TOP_LEVEL = frozenset({"id", "version", "default_effect", "rules"})
_RULE_FIELDS = frozenset(
    {
        "id",
        "effect",
        "actions",
        "actors",
        "scopes",
        "permissions",
        "roles",
        "resource_arguments",
        "reason",
    }
)


class PolicyDocumentError(ValueError):
    pass


class PolicyRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, PolicyEngine] = {}
        self._hashes: dict[str, str] = {}

    def load_directory(self, root: Path) -> None:
        staged = dict(self._engines)
        staged_hashes = dict(self._hashes)
        for path in sorted(root.glob("*.yml")):
            ref, engine = self._load(path)
            if ref in staged:
                raise PolicyDocumentError(f"duplicate policy {ref!r}")
            staged[ref] = engine
            staged_hashes[ref] = hashlib.sha256(path.read_bytes()).hexdigest()
        self._engines = staged
        self._hashes = staged_hashes

    def get(self, ref: str) -> PolicyEngine:
        try:
            return self._engines[ref]
        except KeyError as error:
            raise LookupError(f"unknown policy {ref!r}") from error

    def hash(self, ref: str) -> str:
        self.get(ref)
        return self._hashes[ref]

    @staticmethod
    def _load(path: Path) -> tuple[str, PolicyEngine]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise PolicyDocumentError(f"invalid policy YAML {path}: {error}") from error
        if not isinstance(raw, dict):
            raise PolicyDocumentError(f"policy {path} must be a mapping")
        unknown = raw.keys() - _TOP_LEVEL
        missing = {"id", "version", "default_effect", "rules"} - raw.keys()
        if unknown or missing:
            raise PolicyDocumentError(
                f"policy {path} has unknown={sorted(unknown)} missing={sorted(missing)}"
            )
        if not isinstance(raw["rules"], list):
            raise PolicyDocumentError(f"policy {path} rules must be a list")
        if not isinstance(raw["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", raw["id"]):
            raise PolicyDocumentError(f"policy {path} has an invalid id")
        if not isinstance(raw["version"], str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", raw["version"]
        ):
            raise PolicyDocumentError(f"policy {path} has an invalid version")
        rules: list[PolicyRule] = []
        rule_ids: set[str] = set()
        for item in raw["rules"]:
            if not isinstance(item, dict):
                raise PolicyDocumentError(f"policy {path} rule must be a mapping")
            unknown_rule = item.keys() - _RULE_FIELDS
            missing_rule = {"id", "effect"} - item.keys()
            if unknown_rule or missing_rule:
                raise PolicyDocumentError(
                    f"policy rule has unknown={sorted(unknown_rule)} missing={sorted(missing_rule)}"
                )
            if not isinstance(item["effect"], str):
                raise PolicyDocumentError("policy rule effect must be a string")
            try:
                effect = PolicyEffect(item["effect"])
            except ValueError as error:
                raise PolicyDocumentError(f"invalid policy effect {item['effect']!r}") from error
            rule_id = item["id"]
            if not isinstance(rule_id, str) or not rule_id or rule_id in rule_ids:
                raise PolicyDocumentError(f"policy {path} has an invalid or duplicate rule id")
            rule_ids.add(rule_id)
            reason = item.get("reason", "")
            if not isinstance(reason, str):
                raise PolicyDocumentError("policy rule reason must be a string")
            rules.append(
                PolicyRule(
                    id=rule_id,
                    effect=effect,
                    actions=_string_tuple(item, "actions", ("*",)),
                    actors=_string_tuple(item, "actors", ("*",)),
                    scopes=_string_tuple(item, "scopes", ("*",)),
                    permissions=_string_tuple(item, "permissions", ()),
                    roles=_string_tuple(item, "roles", ()),
                    resource_arguments=_resource_arguments(item),
                    reason=reason,
                )
            )
        if not isinstance(raw["default_effect"], str):
            raise PolicyDocumentError("default policy effect must be a string")
        try:
            default_effect = PolicyEffect(raw["default_effect"])
        except ValueError as error:
            raise PolicyDocumentError(
                f"invalid default policy effect {raw['default_effect']!r}"
            ) from error
        ref = f"{raw['id']}@{raw['version']}"
        return ref, PolicyEngine(tuple(rules), default_effect=default_effect)


def _string_tuple(item: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = item.get(key, default)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(entry, str) and entry for entry in value
    ):
        raise PolicyDocumentError(f"policy rule field {key!r} must be a string list")
    return tuple(value)


def _resource_arguments(item: dict) -> dict[str, str]:
    value = item.get("resource_arguments", {})
    if not isinstance(value, dict) or not all(
        isinstance(argument, str)
        and bool(argument)
        and isinstance(resource, str)
        and bool(resource)
        for argument, resource in value.items()
    ):
        raise PolicyDocumentError(
            "policy rule field 'resource_arguments' must map argument names to context resources"
        )
    return dict(value)
