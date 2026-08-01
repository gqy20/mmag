"""Skill-script verification, sandbox execution, Artifact commit, and audit."""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import zipfile
from importlib.resources import as_file, files
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from ..capabilities import get_capability_context
from ..skill_packages import get_skill_resource_session
from .models import ProcessRequest

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..control_plane import SQLiteControlPlane
    from ..skill_packages import SkillPackage
    from .artifacts import ArtifactRepository
    from .models import ExecutionCommand, ExecutionProfile
    from .process import ProcessRunner
    from .profiles import ExecutionProfileRegistry
    from .workspace import WorkspaceManager


class ScriptExecutionError(RuntimeError):
    code = "script_execution_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ScriptExecutor:
    def __init__(
        self,
        profiles: ExecutionProfileRegistry,
        process_runner: ProcessRunner,
        workspaces: WorkspaceManager,
        artifacts: ArtifactRepository,
        audit_store: SQLiteControlPlane,
    ) -> None:
        self.profiles = profiles
        self.process_runner = process_runner
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.audit_store = audit_store

    async def execute(
        self,
        *,
        profile_ref: str,
        capability: str,
        command_id: str,
        permission: str,
        payload: Mapping[str, Any],
        source_ref: str = "",
        provenance: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        context = get_capability_context()
        session = get_skill_resource_session()
        if context is None or session is None:
            raise ScriptExecutionError("managed execution requires Agent and Skill context")
        skill = session.package
        profile = self.profiles.get(profile_ref)
        input_hash = self._input_hash(payload)
        try:
            command = self._authorize(
                profile,
                skill,
                capability,
                command_id,
                permission,
                context,
            )
            script_hash = self._script_hash(skill, command)
        except ScriptExecutionError as error:
            self._audit_preflight_denial(
                context,
                profile,
                capability,
                input_hash,
                error.code,
            )
            raise
        try:
            result = await self._execute(
                profile,
                command,
                skill,
                capability,
                payload,
                source_ref,
                context.run_id or context.trace_id,
                context.scope,
                script_hash,
                dict(provenance or {}),
            )
        except asyncio.CancelledError:
            self._audit(
                context,
                profile,
                command,
                "cancelled",
                input_hash,
                script_hash,
                {"capability": capability, "error_code": "execution_cancelled"},
            )
            raise
        except Exception as error:
            code = getattr(error, "code", "execution_failed")
            details = {"capability": capability, "error_code": str(code)}
            audit_details = getattr(error, "audit_details", {})
            if isinstance(audit_details, dict):
                details.update(audit_details)
            self._audit(
                context,
                profile,
                command,
                "failed",
                input_hash,
                script_hash,
                details,
            )
            raise ScriptExecutionError(
                f"managed execution failed with {code}",
                code=str(code),
            ) from error
        self._audit(
            context,
            profile,
            command,
            "succeeded",
            input_hash,
            script_hash,
            {"capability": capability, **result["audit"]},
        )
        return result["payload"]

    async def _execute(
        self,
        profile: ExecutionProfile,
        command: ExecutionCommand,
        skill: SkillPackage,
        capability: str,
        payload: Mapping[str, Any],
        source_ref: str,
        run_id: str,
        scope_id: str,
        script_hash: str,
        extra_provenance: Mapping[str, str],
    ) -> dict[str, Any]:
        with self.workspaces.create(run_id) as workspace:
            input_path = self.workspaces.write_input(
                workspace,
                payload,
                max_bytes=profile.limits.max_input_bytes,
            )
            script_path = self._copy_script(workspace, skill, command)
            source_path = self._copy_source(workspace, command, source_ref, scope_id)
            output_path = self.workspaces.output_path(workspace, command.output.filename)
            process_result = await self.process_runner.run(
                ProcessRequest(
                    profile,
                    command,
                    workspace,
                    script_path,
                    input_path,
                    output_path,
                    source_path,
                )
            )
            self._validate_output(
                output_path,
                command.output.kind,
                profile.limits.max_artifact_bytes,
            )
            provenance = {
                **profile.provenance(),
                **skill.snapshot.to_dict(),
                **extra_provenance,
                "execution_capability": capability,
                "execution_command": command.id,
                "execution_script_hash": script_hash,
                "execution_executable_hash": process_result.executable_sha256,
                "execution_argv_hash": process_result.argv_sha256,
            }
            artifact = self.artifacts.commit(
                output_path,
                run_id=run_id,
                scope_id=scope_id,
                output=command.output,
                provenance=provenance,
                max_bytes=profile.limits.max_artifact_bytes,
            )
        return {
            "payload": {
                "status": "succeeded",
                "artifact_ref": artifact.ref,
                "artifacts": [artifact.to_contract()],
                "execution": profile.provenance(),
            },
            "audit": {
                "return_code": process_result.return_code,
                "duration_ms": process_result.duration_ms,
                "stdout_bytes": process_result.stdout_bytes,
                "stderr_bytes": process_result.stderr_bytes,
                "artifact_ref": artifact.ref,
                "artifact_sha256": artifact.sha256,
                "artifact_bytes": artifact.size_bytes,
                "executable_sha256": process_result.executable_sha256,
                "argv_sha256": process_result.argv_sha256,
            },
        }

    @staticmethod
    def _authorize(
        profile: ExecutionProfile,
        skill: SkillPackage,
        capability: str,
        command_id: str,
        permission: str,
        context,
    ) -> ExecutionCommand:
        if profile.ref not in skill.manifest.execution_profiles:
            raise ScriptExecutionError(
                "active Skill does not allow this Execution Profile",
                code="skill_profile_forbidden",
            )
        declared_capabilities = {
            *skill.manifest.capabilities.required,
            *skill.manifest.capabilities.optional,
        }
        if capability not in declared_capabilities:
            raise ScriptExecutionError(
                "active Skill does not declare this capability",
                code="skill_capability_forbidden",
            )
        if capability not in context.allowed_capabilities:
            raise ScriptExecutionError(
                "active Agent request does not allow this capability",
                code="agent_capability_forbidden",
            )
        if profile.ref not in context.allowed_execution_profiles:
            raise ScriptExecutionError(
                "active Agent request does not allow this Execution Profile",
                code="agent_profile_forbidden",
            )
        try:
            command = profile.commands[command_id]
        except KeyError as error:
            raise ScriptExecutionError(
                "Execution Profile does not allow this capability",
                code="profile_command_forbidden",
            ) from error
        if command.permission != permission:
            raise ScriptExecutionError(
                "Execution Profile permission does not match the capability",
                code="profile_permission_mismatch",
            )
        return command

    def _copy_script(
        self,
        workspace,
        skill: SkillPackage,
        command: ExecutionCommand,
    ):
        del skill
        if command.script_ref is None:
            return None
        asset = self._renderer_asset(command.script_ref)
        expected_sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
        with as_file(asset) as path:
            return self.workspaces.copy_asset(
                workspace,
                path,
                expected_sha256=expected_sha256,
            )

    def _copy_source(
        self,
        workspace,
        command: ExecutionCommand,
        source_ref: str,
        scope_id: str,
    ):
        if command.source_kind is None:
            if source_ref:
                raise ScriptExecutionError("command does not accept a source Artifact")
            return None
        if not source_ref:
            raise ScriptExecutionError("command requires a source Artifact")
        artifact, path = self.artifacts.resolve(
            source_ref,
            scope_id=scope_id,
            expected_kind=command.source_kind,
        )
        return self.workspaces.copy_source(
            workspace,
            path,
            filename=f"deck{path.suffix}",
            expected_sha256=artifact.sha256,
        )

    @classmethod
    def _script_hash(cls, skill: SkillPackage, command: ExecutionCommand) -> str:
        del skill
        if command.script_ref is None:
            return ""
        return hashlib.sha256(cls._renderer_asset(command.script_ref).read_bytes()).hexdigest()

    @staticmethod
    def _renderer_asset(ref: str):
        path = PurePosixPath(ref)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ScriptExecutionError("command renderer reference is unsafe")
        asset = files("mmag.renderers").joinpath(*path.parts)
        if not asset.is_file():
            raise ScriptExecutionError("command references an unknown platform renderer")
        return asset

    @staticmethod
    def _validate_output(path, kind: str, max_bytes: int) -> None:
        info = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise ScriptExecutionError("managed output must be a regular non-symlink file")
        if info.st_size < 1 or info.st_size > max_bytes:
            raise ScriptExecutionError("managed output exceeds its Artifact limit")
        try:
            if kind == "slide_deck":
                with zipfile.ZipFile(path) as archive:
                    names = frozenset(archive.namelist())
                    required = {"[Content_Types].xml", "ppt/presentation.xml"}
                    if not required <= names or not any(
                        name.startswith("ppt/slides/slide") and name.endswith(".xml")
                        for name in names
                    ):
                        raise ScriptExecutionError("renderer did not produce a valid PPTX")
                    if archive.testzip() is not None:
                        raise ScriptExecutionError("renderer produced a corrupt PPTX")
            elif kind == "presentation_pdf":
                if not path.read_bytes()[:5] == b"%PDF-":
                    raise ScriptExecutionError("renderer did not produce a valid PDF")
            elif kind == "presentation_source":
                if not path.read_text(encoding="utf-8").startswith("---\n"):
                    raise ScriptExecutionError("renderer did not produce normalized Markdown")
            elif kind == "presentation_preview_svg":
                content = path.read_text(encoding="utf-8")
                if not content.lstrip().startswith("<svg") or "</svg>" not in content:
                    raise ScriptExecutionError("renderer did not produce a valid SVG preview")
            elif kind == "presentation_preview":
                with path.open("rb") as handle:
                    header = handle.read(24)
                if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) != 24:
                    raise ScriptExecutionError("renderer did not produce a valid PNG preview")
                width, height = struct.unpack(">II", header[16:24])
                if width < 640 or height < 360 or abs(width / height - 16 / 9) > 0.03:
                    raise ScriptExecutionError("presentation preview has invalid dimensions")
        except (UnicodeDecodeError, zipfile.BadZipFile) as error:
            raise ScriptExecutionError(f"renderer produced invalid {kind} content") from error

    @staticmethod
    def _input_hash(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _audit(
        self,
        context,
        profile: ExecutionProfile,
        command: ExecutionCommand,
        decision: str,
        input_hash: str,
        script_hash: str,
        extra: dict[str, Any],
    ) -> None:
        self.audit_store.append_audit(
            "execution.process",
            actor_id=context.actor_id,
            scope_id=context.scope,
            trace_id=context.trace_id,
            target=command.id,
            decision=decision,
            details={
                **profile.provenance(),
                "input_sha256": input_hash,
                "script_ref": command.script_ref or "",
                "script_sha256": script_hash,
                "limits": {
                    "timeout_seconds": profile.limits.timeout_seconds,
                    "cpu_seconds": profile.limits.cpu_seconds,
                    "memory_bytes": profile.limits.memory_bytes,
                    "max_processes": profile.limits.max_processes,
                    "max_input_bytes": profile.limits.max_input_bytes,
                    "max_output_bytes": profile.limits.max_output_bytes,
                    "max_artifact_bytes": profile.limits.max_artifact_bytes,
                },
                **extra,
            },
        )

    def _audit_preflight_denial(
        self,
        context,
        profile: ExecutionProfile,
        capability: str,
        input_hash: str,
        error_code: str,
    ) -> None:
        self.audit_store.append_audit(
            "execution.process",
            actor_id=context.actor_id,
            scope_id=context.scope,
            trace_id=context.trace_id,
            target=capability,
            decision="denied",
            details={
                **profile.provenance(),
                "input_sha256": input_hash,
                "error_code": error_code,
                "phase": "preflight",
            },
        )
