"""Per-run workspaces with narrow writable areas and deterministic cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import ExecutionWorkspace

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, root: Path, *, retention_seconds: int = 3600) -> None:
        self.root = root.resolve()
        self.retention_seconds = retention_seconds
        self.runs = self.root / "runs"
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runs, 0o700)

    @contextmanager
    def create(self, run_id: str) -> Iterator[ExecutionWorkspace]:
        prefix = hashlib.sha256(run_id.encode()).hexdigest()[:12]
        root = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=self.runs))
        os.chmod(root, 0o700)
        workspace = ExecutionWorkspace(
            root,
            self._directory(root, "input", 0o700),
            self._directory(root, "assets", 0o700),
            self._directory(root, "tmp", 0o700),
            self._directory(root, "staging", 0o700),
        )
        try:
            yield workspace
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def write_input(
        self,
        workspace: ExecutionWorkspace,
        payload: Mapping[str, Any],
        *,
        max_bytes: int,
    ) -> Path:
        path = self._child(workspace.inputs, "input.json")
        data = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(data) > max_bytes:
            raise WorkspaceError("managed execution input exceeds its byte limit")
        self._write_new(path, data, 0o400)
        return path

    def copy_asset(
        self,
        workspace: ExecutionWorkspace,
        source: Path,
        *,
        expected_sha256: str,
    ) -> Path:
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("execution asset must be a regular non-symlink file")
        source = source.resolve(strict=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise WorkspaceError("execution asset hash does not match its Skill Package")
        name = f"{expected_sha256[:16]}-{source.name}"
        target = self._child(workspace.assets, name)
        self._write_new(target, data, 0o400)
        return target

    def copy_source(
        self,
        workspace: ExecutionWorkspace,
        source: Path,
        *,
        filename: str,
        expected_sha256: str,
    ) -> Path:
        if filename != Path(filename).name:
            raise WorkspaceError("source filename must be a basename")
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceError("source artifact must be a regular non-symlink file")
        source = source.resolve(strict=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise WorkspaceError("source artifact hash mismatch")
        target = self._child(workspace.inputs, filename)
        self._write_new(target, data, 0o400)
        return target

    def output_path(self, workspace: ExecutionWorkspace, filename: str) -> Path:
        if filename != Path(filename).name:
            raise WorkspaceError("output filename must be a basename")
        return self._child(workspace.staging, filename)

    def cleanup_stale(self, *, now: float | None = None) -> int:
        threshold = (time.time() if now is None else now) - self.retention_seconds
        removed = 0
        for path in self.runs.iterdir():
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                continue
            if info.st_mtime >= threshold:
                continue
            shutil.rmtree(path)
            removed += 1
        return removed

    @staticmethod
    def _directory(root: Path, name: str, mode: int) -> Path:
        path = root / name
        path.mkdir(mode=mode)
        return path

    @staticmethod
    def _child(parent: Path, name: str) -> Path:
        path = parent / name
        if path.parent != parent or path.is_symlink():
            raise WorkspaceError("workspace path escapes its declared directory")
        return path

    @staticmethod
    def _write_new(path: Path, data: bytes, mode: int) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
