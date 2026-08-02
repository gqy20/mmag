"""Governed Deep Agents workspace with an explicitly unsafe local Demo provider."""

from __future__ import annotations

import os
import signal
import subprocess
import uuid
from typing import TYPE_CHECKING, Any

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

from ..capabilities import (
    AuthorizationDecision,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilitySpec,
)
from ..logger import get_logger, log_event, safe_hash

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..runtimes import RunRequest
    from .artifacts import ArtifactRepository
    from .models import ExecutionProfile, ExecutionWorkspace
    from .profiles import ExecutionProfileRegistry
    from .workspace import WorkspaceManager

log = get_logger(__name__)
WORKSPACE_CAPABILITIES = frozenset(
    {"workspace.read", "workspace.write", "workspace.execute", "workspace.commit"}
)


class WorkspaceBackendError(RuntimeError):
    pass


def create_workspace_capabilities(
    commit_handler: Callable[..., Any] | None = None,
) -> tuple[CapabilitySpec, ...]:
    """Return canonical Specs used by Package validation and Backend authorization."""

    def backend_only(**_: Any) -> None:
        raise WorkspaceBackendError("workspace capabilities are available only through Backend tools")

    commit = commit_handler or backend_only

    common_path = {
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["operation", "path"],
    }
    return (
        CapabilitySpec(
            "workspace.read",
            "Read files in the current managed Run workspace.",
            common_path,
            backend_only,
            permission="workspace:file:read",
        ),
        CapabilitySpec(
            "workspace.write",
            "Write files in the current managed Run workspace.",
            common_path,
            backend_only,
            effect=CapabilityEffect.WRITE,
            permission="workspace:file:write",
        ),
        CapabilitySpec(
            "workspace.execute",
            "Execute a command in the current managed Run workspace.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            backend_only,
            effect=CapabilityEffect.WRITE,
            permission="workspace:process:execute",
        ),
        CapabilitySpec(
            "workspace.commit",
            "Commit a declared file from the current Run workspace as an Artifact.",
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"filename": {"type": "string"}},
                "required": ["filename"],
            },
            commit,
            effect=CapabilityEffect.WRITE,
            permission="artifact:generate",
        ),
    )


class LocalExecutionBackend(FilesystemBackend, SandboxBackendProtocol):
    """Full local shell for trusted demos; this is deliberately not a sandbox."""

    def __init__(self, workspace: ExecutionWorkspace, profile: ExecutionProfile) -> None:
        super().__init__(workspace.temporary, virtual_mode=True, max_file_size_mb=10)
        self.workspace = workspace
        self.profile = profile
        self._id = f"unsafe-local-{uuid.uuid4().hex[:10]}"

    @property
    def id(self) -> str:
        return self._id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command.strip():
            return ExecuteResponse("Error: command must not be empty", 2)
        if timeout is not None and timeout <= 0:
            return ExecuteResponse("Error: timeout must be positive", 2)
        deadline = timeout if timeout is not None else self.profile.limits.timeout_seconds
        deadline = min(deadline, self.profile.limits.timeout_seconds)
        environment = {
            "HOME": str(self.workspace.temporary),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": str(self.workspace.temporary),
            **dict(self.profile.environment),
        }
        try:
            process = subprocess.Popen(  # noqa: S603
                ["/bin/bash", "-c", command],
                shell=False,
                cwd=self.workspace.temporary,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            output, truncated = self._bounded_output(stdout, stderr)
            return ExecuteResponse(f"{output}\nCommand timed out after {deadline}s".strip(), 124, truncated)
        output, truncated = self._bounded_output(stdout, stderr)
        return ExecuteResponse(output or "<no output>", process.returncode, truncated)

    def _bounded_output(self, stdout: bytes, stderr: bytes) -> tuple[str, bool]:
        combined = stdout + (b"\n[stderr] " + stderr if stderr else b"")
        maximum = self.profile.limits.max_output_bytes
        truncated = len(combined) > maximum
        return combined[:maximum].decode("utf-8", errors="replace"), truncated


class GovernedWorkspaceBackend(CompositeBackend, SandboxBackendProtocol):
    """Route State files and a real workspace while enforcing canonical Policy."""

    def __init__(
        self,
        local: LocalExecutionBackend,
        executor: CapabilityExecutor,
        specs: tuple[CapabilitySpec, ...],
    ) -> None:
        super().__init__(StateBackend(), {"/workspace/": local})
        self.local = local
        self.executor = executor
        self.specs = {spec.name: spec for spec in specs}

    @property
    def id(self) -> str:
        return self.local.id

    def ls(self, path: str) -> LsResult:
        denied = self._authorize_path("workspace.read", "ls", path)
        return LsResult(error=denied) if denied else super().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        denied = self._authorize_path("workspace.read", "read", file_path)
        return ReadResult(error=denied) if denied else super().read(file_path, offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        denied = self._authorize_path("workspace.read", "glob", path or "/workspace")
        return GlobResult(error=denied) if denied else super().glob(pattern, path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        denied = self._authorize_path("workspace.read", "grep", path or "/workspace")
        if denied:
            return GrepResult(error=denied)
        return super().grep(pattern, path, glob, max_count=max_count)

    def write(self, file_path: str, content: str) -> WriteResult:
        if not self._is_workspace_path(file_path):
            return WriteResult(error="Permission denied: writes require /workspace")
        denied = self._authorize_path("workspace.write", "write", file_path)
        return WriteResult(error=denied) if denied else super().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not self._is_workspace_path(file_path):
            return EditResult(error="Permission denied: edits require /workspace")
        denied = self._authorize_path("workspace.write", "edit", file_path)
        if denied:
            return EditResult(error=denied)
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        if not self._is_workspace_path(file_path):
            return DeleteResult(error="Permission denied: deletion requires /workspace")
        denied = self._authorize_path("workspace.write", "delete", file_path)
        return DeleteResult(error=denied) if denied else super().delete(file_path)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        arguments = {"command": command, "timeout": timeout}
        denied = self._authorize("workspace.execute", arguments)
        if denied:
            return ExecuteResponse(denied, 126)
        result = self.local.execute(command, timeout=timeout)
        log_event(
            log,
            "workspace.command_completed",
            status="succeeded" if result.exit_code == 0 else "failed",
            backend=self.id,
            command_sha256=safe_hash(command),
            exit_code=result.exit_code,
            output_truncated=result.truncated,
        )
        return result

    def _authorize_path(self, capability: str, operation: str, path: str) -> str | None:
        if not self._is_workspace_path(path):
            return None
        return self._authorize(capability, {"operation": operation, "path": path})

    @staticmethod
    def _is_workspace_path(path: str) -> bool:
        return path == "/workspace" or path.startswith("/workspace/")

    def _authorize(self, capability: str, arguments: dict[str, Any]) -> str | None:
        decision = self.executor.authorize(self.specs[capability], arguments)
        log_event(
            log,
            "workspace.policy_decided",
            status=decision.decision.value,
            capability=capability,
            input_sha256=safe_hash(arguments),
        )
        if decision.decision is AuthorizationDecision.DENY:
            return f"Permission denied: {decision.reason}"
        # REQUIRE_APPROVAL reaches the backend only after native HITL resumes.
        return None


class WorkspaceBackendFactory:
    """Resolve Package profile constraints and build one Backend per stable Run."""

    def __init__(
        self,
        profiles: ExecutionProfileRegistry,
        workspaces: WorkspaceManager,
        executor: CapabilityExecutor,
        artifacts: ArtifactRepository | None = None,
        *,
        allow_unsafe_local: bool = False,
    ) -> None:
        self.profiles = profiles
        self.workspaces = workspaces
        self.executor = executor
        self.artifacts = artifacts
        self.allow_unsafe_local = allow_unsafe_local
        self.specs = create_workspace_capabilities(self.commit)

    @property
    def capabilities(self) -> tuple[CapabilitySpec, ...]:
        return self.specs

    def create(self, request: RunRequest) -> GovernedWorkspaceBackend | StateBackend:
        allowed = frozenset(filter(None, request.metadata.get("capabilities", "").split(",")))
        if not WORKSPACE_CAPABILITIES.intersection(allowed):
            return StateBackend()
        if not self.allow_unsafe_local:
            log_event(
                log,
                "workspace.disabled",
                status="safe-only",
                run_id=request.context.run_id,
            )
            return StateBackend()
        profile_refs = tuple(filter(None, request.metadata.get("execution_profiles", "").split(",")))
        if len(profile_refs) != 1:
            raise WorkspaceBackendError("workspace execution requires exactly one Execution Profile")
        profile = self.profiles.get(profile_refs[0])
        if profile.runner != "host":
            raise WorkspaceBackendError("local Demo backend requires an explicit host profile")
        workspace = self.workspaces.acquire(request.context.run_id)
        log_event(
            log,
            "workspace.created",
            status="unsafe-local",
            run_id=request.context.run_id,
            execution_profile=profile.ref,
            workspace_id=workspace.root.name,
        )
        return GovernedWorkspaceBackend(
            LocalExecutionBackend(workspace, profile),
            self.executor,
            self.specs,
        )

    def is_enabled(self, request: RunRequest) -> bool:
        allowed = request.metadata.get("capabilities", "").split(",")
        return self.allow_unsafe_local and bool(WORKSPACE_CAPABILITIES.intersection(allowed))

    def release(self, request: RunRequest) -> None:
        allowed = request.metadata.get("capabilities", "").split(",")
        if not WORKSPACE_CAPABILITIES.intersection(allowed) or not request.context.run_id:
            return
        workspace = self.workspaces.acquire(request.context.run_id)
        self.workspaces.release(workspace)
        log_event(
            log,
            "workspace.released",
            status="retained",
            run_id=request.context.run_id,
            workspace_id=workspace.root.name,
        )

    async def commit(self, filename: str) -> dict[str, Any]:
        """Commit only an output filename declared by the active Execution Profile."""
        from ..capabilities import get_capability_context

        context = get_capability_context()
        if context is None or not context.run_id or not context.scope:
            raise WorkspaceBackendError("workspace commit requires trusted Run and Scope context")
        if self.artifacts is None:
            raise WorkspaceBackendError("Artifact Repository is not configured")
        if filename != self._basename(filename):
            raise WorkspaceBackendError("workspace commit filename must be a basename")
        profile = self._active_profile(context.allowed_execution_profiles)
        output = self._declared_output(profile, filename)
        workspace = self.workspaces.acquire(context.run_id)
        staged = workspace.temporary / filename
        from .artifacts import validate_artifact_output

        validate_artifact_output(staged, output.kind, profile.limits.max_artifact_bytes)
        artifact = self.artifacts.commit(
            staged,
            run_id=context.run_id,
            scope_id=context.scope,
            output=output,
            provenance={
                **profile.provenance(),
                "execution_mode": "unsafe-local",
                "workspace_output": filename,
            },
            max_bytes=profile.limits.max_artifact_bytes,
            idempotency_key=f"workspace:{context.run_id}:{context.scope}:{filename}",
        )
        log_event(
            log,
            "workspace.artifact_committed",
            status="succeeded",
            run_id=context.run_id,
            artifact_ref=artifact.ref,
            artifact_kind=artifact.kind,
            artifact_sha256=artifact.sha256,
        )
        return {
            "artifact_ref": artifact.ref,
            "artifacts": [artifact.to_contract()],
        }

    def _active_profile(self, allowed: frozenset[str]) -> ExecutionProfile:
        if len(allowed) != 1:
            raise WorkspaceBackendError("workspace commit requires exactly one Execution Profile")
        return self.profiles.get(next(iter(allowed)))

    @staticmethod
    def _declared_output(profile: ExecutionProfile, filename: str):
        outputs = tuple(
            command.output for command in profile.commands.values() if command.output.filename == filename
        )
        if len(outputs) != 1:
            raise WorkspaceBackendError("file is not a unique declared Execution Profile output")
        return outputs[0]

    @staticmethod
    def _basename(filename: str) -> str:
        return filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
