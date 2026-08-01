"""Immutable contracts for the controlled execution plane."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionMetadata:
    name: str
    version: str
    description: str

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    timeout_seconds: int
    cpu_seconds: int
    memory_bytes: int
    max_processes: int
    max_input_bytes: int
    max_output_bytes: int
    max_artifact_bytes: int


@dataclass(frozen=True, slots=True)
class ExecutionOutput:
    filename: str
    kind: str
    media_type: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class ExecutionCommand:
    id: str
    permission: str
    executable: str
    script_ref: str | None
    argv: tuple[str, ...]
    source_kind: str | None
    output: ExecutionOutput


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    root: Path
    metadata: ExecutionMetadata
    runner: str
    image_digest: str
    network: str
    environment: Mapping[str, str]
    read_only_paths: tuple[str, ...]
    writable_areas: tuple[str, ...]
    limits: ExecutionLimits
    commands: Mapping[str, ExecutionCommand]
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))

    @property
    def ref(self) -> str:
        return self.metadata.ref

    def provenance(self) -> dict[str, str]:
        return {
            "execution_profile": self.ref,
            "execution_profile_hash": self.sha256,
            "execution_image_digest": self.image_digest,
            "execution_runner": self.runner,
            "execution_network": self.network,
        }


@dataclass(frozen=True, slots=True)
class ExecutionWorkspace:
    root: Path
    inputs: Path
    assets: Path
    temporary: Path
    staging: Path


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    profile: ExecutionProfile
    command: ExecutionCommand
    workspace: ExecutionWorkspace
    script: Path | None
    input_path: Path
    output_path: Path
    source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ProcessResult:
    return_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    executable_sha256: str
    argv_sha256: str


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    id: str
    ref: str
    run_id: str
    scope_id: str
    kind: str
    schema_version: str
    filename: str
    media_type: str
    sha256: str
    size_bytes: int
    relative_path: str
    provenance: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_contract(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "ref": self.ref,
            "filename": self.filename,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "provenance": dict(self.provenance),
        }
