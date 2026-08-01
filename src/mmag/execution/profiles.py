"""Strict loading and atomic registration of Execution Profiles."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .models import (
    ExecutionCommand,
    ExecutionLimits,
    ExecutionMetadata,
    ExecutionOutput,
    ExecutionProfile,
)

_PLACEHOLDERS = frozenset(
    {"{script}", "{input}", "{output}", "{source}", "{staging}", "{temporary}"}
)
_EXECUTABLES = frozenset({"python", "libreoffice"})
_READ_ONLY_PATHS = frozenset({"/usr", "/lib", "/lib64", "/etc/fonts"})
_FORBIDDEN_COMMANDS = frozenset({"shell.exec", "python.eval", "python.exec"})
_ENVIRONMENT_NAMES = frozenset({"LANG", "LC_ALL", "PYTHONHASHSEED", "TZ"})


class ExecutionProfileError(ValueError):
    """A profile cannot be trusted or executed."""


def _schema() -> dict[str, Any]:
    resource = files("mmag.execution").joinpath("schemas/execution-profile-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


class ExecutionProfileLoader:
    def __init__(self) -> None:
        self._validator = Draft202012Validator(_schema())

    def load(self, path: Path) -> ExecutionProfile:
        raw_bytes = path.read_bytes()
        try:
            raw = yaml.safe_load(raw_bytes)
        except yaml.YAMLError as error:
            raise ExecutionProfileError(
                f"invalid Execution Profile YAML {path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise ExecutionProfileError("Execution Profile must be a mapping")
        try:
            self._validator.validate(raw)
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ExecutionProfileError(
                f"Execution Profile failed at {location}: {error.message}"
            ) from error
        command_ids = [item["id"] for item in raw["spec"]["commands"]]
        if len(command_ids) != len(set(command_ids)):
            raise ExecutionProfileError("Execution Profile command ids must be unique")
        profile = self._parse(path, raw, hashlib.sha256(raw_bytes).hexdigest())
        self._validate_semantics(profile, path)
        return profile

    @staticmethod
    def _parse(path: Path, raw: dict[str, Any], sha256: str) -> ExecutionProfile:
        metadata = raw["metadata"]
        spec = raw["spec"]
        resources = spec["resources"]
        commands = {
            item["id"]: ExecutionCommand(
                item["id"],
                item["permission"],
                item["executable"],
                item.get("script_ref"),
                tuple(item["argv"]),
                item.get("source_kind"),
                ExecutionOutput(
                    item["output"]["filename"],
                    item["output"]["kind"],
                    item["output"]["media_type"],
                    item["output"]["schema_version"],
                ),
            )
            for item in spec["commands"]
        }
        return ExecutionProfile(
            path.resolve(),
            ExecutionMetadata(metadata["name"], metadata["version"], metadata["description"]),
            spec["runner"]["kind"],
            spec["runner"]["image_digest"],
            spec["network"]["mode"],
            spec["environment"]["set"],
            tuple(spec["filesystem"]["read_only"]),
            tuple(spec["filesystem"]["writable"]),
            ExecutionLimits(
                resources["timeout_seconds"],
                resources["cpu_seconds"],
                resources["memory_bytes"],
                resources["max_processes"],
                resources["max_input_bytes"],
                resources["max_output_bytes"],
                resources["max_artifact_bytes"],
            ),
            commands,
            sha256,
        )

    @staticmethod
    def _validate_semantics(profile: ExecutionProfile, path: Path) -> None:
        if path.stem != profile.metadata.name:
            raise ExecutionProfileError(
                f"Execution Profile filename {path.stem!r} does not match {profile.metadata.name!r}"
            )
        if set(profile.read_only_paths) - _READ_ONLY_PATHS:
            raise ExecutionProfileError("Execution Profile requests an untrusted read-only path")
        if profile.writable_areas != ("temporary", "staging"):
            raise ExecutionProfileError("only temporary and staging may be writable")
        if set(profile.environment) - _ENVIRONMENT_NAMES:
            raise ExecutionProfileError("Execution Profile sets an unsafe environment variable")
        if len(profile.commands) == 0:
            raise ExecutionProfileError("Execution Profile must declare commands")
        for command in profile.commands.values():
            ExecutionProfileLoader._validate_command(command)

    @staticmethod
    def _validate_command(command: ExecutionCommand) -> None:
        if command.id in _FORBIDDEN_COMMANDS:
            raise ExecutionProfileError(f"forbidden general execution command {command.id!r}")
        if command.executable not in _EXECUTABLES:
            raise ExecutionProfileError(f"untrusted executable alias {command.executable!r}")
        if command.executable == "python":
            if command.script_ref is None or command.argv[:3] != ("-I", "-B", "{script}"):
                raise ExecutionProfileError(
                    f"Python command {command.id!r} must run a registered script in isolated mode"
                )
            if any(token in {"-c", "-m"} for token in command.argv):
                raise ExecutionProfileError(
                    f"Python command {command.id!r} cannot execute dynamic code or modules"
                )
        elif command.script_ref is not None:
            raise ExecutionProfileError(
                f"non-Python command {command.id!r} cannot bind a Skill script"
            )
        if command.output.filename != Path(command.output.filename).name:
            raise ExecutionProfileError("execution output filename must be a basename")
        tokens = set(command.argv)
        unknown = {
            token
            for token in tokens
            if ("{" in token or "}" in token) and token not in _PLACEHOLDERS
        }
        if unknown:
            raise ExecutionProfileError(
                f"command {command.id!r} contains unknown argv placeholders: {sorted(unknown)}"
            )
        if command.script_ref is not None and ("{input}" not in tokens or "{output}" not in tokens):
            raise ExecutionProfileError(f"script command {command.id!r} must bind input and output")
        if (command.script_ref is None) != ("{script}" not in tokens):
            raise ExecutionProfileError(f"command {command.id!r} has an invalid script binding")
        if command.source_kind is None and "{source}" in tokens:
            raise ExecutionProfileError(f"command {command.id!r} has an undeclared source")
        if command.source_kind is not None and "{source}" not in tokens:
            raise ExecutionProfileError(f"command {command.id!r} does not bind its source")
        if command.source_kind is not None and "{staging}" not in tokens:
            raise ExecutionProfileError(f"command {command.id!r} does not bind staging")
        if any("\x00" in token for token in command.argv):
            raise ExecutionProfileError(f"command {command.id!r} contains a NUL byte")


class ExecutionProfileRegistry:
    def __init__(self, loader: ExecutionProfileLoader | None = None) -> None:
        self.loader = loader or ExecutionProfileLoader()
        self._profiles: dict[str, ExecutionProfile] = {}

    def load_directory(self, root: Path) -> tuple[ExecutionProfile, ...]:
        staged: dict[str, ExecutionProfile] = {}
        for path in sorted(root.glob("*.yml")):
            profile = self.loader.load(path)
            if profile.ref in staged:
                raise ExecutionProfileError(f"duplicate Execution Profile {profile.ref!r}")
            staged[profile.ref] = profile
        self._profiles = staged
        return self.list()

    def get(self, ref: str) -> ExecutionProfile:
        try:
            return self._profiles[ref]
        except KeyError as error:
            raise LookupError(f"unknown Execution Profile {ref!r}") from error

    def list(self) -> tuple[ExecutionProfile, ...]:
        return tuple(self._profiles[ref] for ref in sorted(self._profiles))
