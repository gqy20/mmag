"""Fail-closed fixed-argv process execution inside Bubblewrap."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import shutil
import signal
import stat
import time
from contextlib import suppress
from pathlib import Path

from .models import ProcessRequest, ProcessResult


class ProcessExecutionError(RuntimeError):
    code = "execution_failed"


class SandboxUnavailableError(ProcessExecutionError):
    code = "sandbox_unavailable"


class ProcessTimeoutError(ProcessExecutionError):
    code = "execution_timeout"


class ProcessOutputLimitError(ProcessExecutionError):
    code = "output_limit"


class ProcessFailedError(ProcessExecutionError):
    code = "process_failed"

    def __init__(self, return_code: int) -> None:
        super().__init__(f"managed process exited with code {return_code}")
        self.return_code = return_code


class ProcessRunner:
    """Execute only an already-validated ExecutionCommand."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        bubblewrap_path: str | None = None,
    ) -> None:
        self.runtime_root = runtime_root.resolve()
        self.bubblewrap_path = bubblewrap_path or shutil.which("bwrap") or ""

    async def run(self, request: ProcessRequest) -> ProcessResult:
        argv, executable_path = self.build_argv(request)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
            start_new_session=True,
            preexec_fn=self._limits(request),
        )
        try:
            stdout, stderr, return_code = await self._communicate(process, request)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(process))
            raise
        except TimeoutError as error:
            await self._terminate(process)
            raise ProcessTimeoutError("managed process exceeded its deadline") from error
        except ProcessOutputLimitError:
            await self._terminate(process)
            raise
        except Exception:
            await self._terminate(process)
            raise
        if return_code != 0:
            raise ProcessFailedError(return_code)
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        logical_argv = json.dumps(
            {
                "profile": request.profile.ref,
                "command": request.command.id,
                "argv": request.command.argv,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return ProcessResult(
            return_code,
            duration_ms,
            len(stdout),
            len(stderr),
            self._sha256(executable_path),
            hashlib.sha256(logical_argv).hexdigest(),
        )

    def build_argv(self, request: ProcessRequest) -> tuple[tuple[str, ...], Path]:
        profile = request.profile
        if profile.runner != "bubblewrap" or profile.network != "none":
            raise SandboxUnavailableError("Execution Profile does not enforce an offline sandbox")
        bwrap = Path(self.bubblewrap_path)
        if not bwrap.is_absolute() or not bwrap.is_file():
            raise SandboxUnavailableError("bubblewrap is not installed")
        self._validate_paths(request)
        executable_path, sandbox_executable = self._resolve_executable(request.command.executable)
        mappings = self._sandbox_mappings(request)
        command_argv = tuple(mappings.get(token, token) for token in request.command.argv)
        argv: list[str] = [
            str(bwrap),
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--cap-drop",
            "ALL",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        argv.extend(self._read_only_mounts(profile.read_only_paths))
        argv.extend(("--ro-bind", str(self.runtime_root), "/runtime"))
        argv.extend(("--dir", "/work"))
        argv.extend(("--ro-bind", str(request.workspace.inputs), "/work/input"))
        argv.extend(("--ro-bind", str(request.workspace.assets), "/assets"))
        argv.extend(("--bind", str(request.workspace.temporary), "/work/tmp"))
        argv.extend(("--bind", str(request.workspace.staging), "/work/staging"))
        argv.extend(("--chdir", "/work/tmp"))
        environment = {
            **profile.environment,
            "HOME": "/tmp",
            "PATH": "/runtime/bin:/usr/bin",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": "/work/tmp",
        }
        for name, value in sorted(environment.items()):
            argv.extend(("--setenv", name, value))
        argv.append(sandbox_executable)
        argv.extend(command_argv)
        return tuple(argv), executable_path

    @staticmethod
    def _validate_paths(request: ProcessRequest) -> None:
        workspace = request.workspace
        root = workspace.root.resolve(strict=True)
        for directory in (
            workspace.inputs,
            workspace.assets,
            workspace.temporary,
            workspace.staging,
        ):
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SandboxUnavailableError("workspace contains an unsafe directory")
            if directory.resolve(strict=True).parent != root:
                raise SandboxUnavailableError("workspace directory escapes its run root")
        for path, parent in (
            (request.input_path, workspace.inputs),
            (request.script, workspace.assets),
            (request.source_path, workspace.inputs),
        ):
            if path is None:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SandboxUnavailableError("managed input is not a regular file")
            if path.resolve(strict=True).parent != parent.resolve(strict=True):
                raise SandboxUnavailableError("managed input escapes its declared directory")
        if request.output_path.parent != workspace.staging or request.output_path.is_symlink():
            raise SandboxUnavailableError("managed output escapes Artifact staging")

    def _resolve_executable(self, alias: str) -> tuple[Path, str]:
        if alias == "python":
            executable = self.runtime_root / "bin" / "python"
            sandbox_path = "/runtime/bin/python"
        elif alias == "libreoffice":
            resolved = shutil.which("libreoffice") or shutil.which("soffice")
            if not resolved:
                raise SandboxUnavailableError("LibreOffice is not installed")
            executable = Path(resolved).resolve()
            sandbox_path = str(executable)
        else:
            raise SandboxUnavailableError(f"untrusted executable alias {alias!r}")
        if not executable.is_file():
            raise SandboxUnavailableError(f"managed executable {alias!r} is unavailable")
        return executable, sandbox_path

    @staticmethod
    def _sandbox_mappings(request: ProcessRequest) -> dict[str, str]:
        mappings = {
            "{input}": f"/work/input/{request.input_path.name}",
            "{output}": f"/work/staging/{request.output_path.name}",
            "{staging}": "/work/staging",
            "{temporary}": "/work/tmp",
        }
        if request.script is not None:
            mappings["{script}"] = f"/assets/{request.script.name}"
        if request.source_path is not None:
            mappings["{source}"] = f"/work/input/{request.source_path.name}"
        return mappings

    @staticmethod
    def _read_only_mounts(paths: tuple[str, ...]) -> tuple[str, ...]:
        arguments: list[str] = []
        for raw in paths:
            path = Path(raw)
            if path.exists():
                arguments.extend(("--ro-bind", raw, raw))
        return tuple(arguments)

    @staticmethod
    def _limits(request: ProcessRequest):
        limits = request.profile.limits

        def apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (limits.max_artifact_bytes, limits.max_artifact_bytes),
            )
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

        return apply

    async def _communicate(
        self,
        process: asyncio.subprocess.Process,
        request: ProcessRequest,
    ) -> tuple[bytes, bytes, int]:
        if process.stdout is None or process.stderr is None:
            raise ProcessExecutionError("managed process streams are unavailable")
        budget = [request.profile.limits.max_output_bytes]
        tasks = (
            asyncio.create_task(self._read_limited(process.stdout, budget)),
            asyncio.create_task(self._read_limited(process.stderr, budget)),
            asyncio.create_task(process.wait()),
        )
        try:
            async with asyncio.timeout(request.profile.limits.timeout_seconds):
                stdout, stderr, return_code = await asyncio.gather(*tasks)
                return stdout, stderr, return_code
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader,
        budget: list[int],
    ) -> bytes:
        chunks: list[bytes] = []
        while chunk := await stream.read(8192):
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise ProcessOutputLimitError("managed process output exceeded its byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    @staticmethod
    def _sha256(path: Path) -> str:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SandboxUnavailableError("managed executable is not a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
