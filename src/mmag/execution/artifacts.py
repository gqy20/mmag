"""Atomic file-backed Artifact Repository with SQLite metadata."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..control_plane import Artifact
from .models import ExecutionOutput, StoredArtifact

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..control_plane import SQLiteControlPlane


class ArtifactRepositoryError(RuntimeError):
    pass


class ArtifactRepository:
    def __init__(self, root: Path, store: SQLiteControlPlane) -> None:
        self.root = root.resolve()
        self.store = store
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.reconciliation = self.reconcile()

    def reconcile(self) -> tuple[int, int]:
        """Remove interrupted commits/orphans and reject broken persisted refs."""
        records = self.store.list_artifacts()
        expected_directories: set[Path] = set()
        for artifact in records:
            relative = Path(artifact.content)
            if (
                relative.is_absolute()
                or len(relative.parts) != 3
                or relative.parts[0] != artifact.id[:2]
                or relative.parts[1] != artifact.id
                or not self._valid_artifact_id(artifact.id)
            ):
                raise ArtifactRepositoryError("Artifact record contains an unsafe path")
            shard = self.root / relative.parts[0]
            directory = shard / relative.parts[1]
            expected_directories.add(directory)
            candidate = self.root / relative
            if (
                shard.is_symlink()
                or directory.is_symlink()
                or candidate.is_symlink()
                or not candidate.is_file()
            ):
                raise ArtifactRepositoryError(
                    f"Artifact record {artifact.id!r} has no trusted file"
                )

        incomplete = 0
        orphaned = 0
        for shard in self.root.iterdir():
            if shard.is_symlink() or not shard.is_dir():
                continue
            for candidate in shard.iterdir():
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
                if candidate.name.startswith(".commit-"):
                    shutil.rmtree(candidate)
                    incomplete += 1
                elif self._looks_like_artifact_directory(shard, candidate):
                    if candidate not in expected_directories:
                        shutil.rmtree(candidate)
                        orphaned += 1
        return incomplete, orphaned

    def commit(
        self,
        staged: Path,
        *,
        run_id: str,
        scope_id: str,
        output: ExecutionOutput,
        provenance: Mapping[str, str],
        max_bytes: int,
    ) -> StoredArtifact:
        data_hash, size = self._inspect(staged, max_bytes=max_bytes)
        artifact_id = uuid.uuid4().hex
        shard = self.root / artifact_id[:2]
        shard.mkdir(mode=0o700, exist_ok=True)
        final_directory = shard / artifact_id
        temporary = Path(tempfile.mkdtemp(prefix=".commit-", dir=shard))
        final_file = temporary / output.filename
        try:
            self._copy(staged, final_file)
            copied_hash, copied_size = self._inspect(final_file, max_bytes=max_bytes)
            if copied_hash != data_hash or copied_size != size:
                raise ArtifactRepositoryError("Artifact changed while it was being committed")
            os.replace(temporary, final_directory)
            relative = (final_directory / output.filename).relative_to(self.root).as_posix()
            stored = StoredArtifact(
                artifact_id,
                f"artifact://{artifact_id}",
                run_id,
                scope_id,
                output.kind,
                output.schema_version,
                output.filename,
                output.media_type,
                data_hash,
                size,
                relative,
                provenance,
            )
            self.store.create_artifact(
                Artifact(
                    artifact_id,
                    run_id,
                    scope_id,
                    output.kind,
                    relative,
                    {
                        "schema_version": output.schema_version,
                        "filename": output.filename,
                        "media_type": output.media_type,
                        "sha256": data_hash,
                        "size_bytes": size,
                        "provenance": dict(provenance),
                    },
                )
            )
            return stored
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(final_directory, ignore_errors=True)
            raise

    def resolve(
        self,
        ref: str,
        *,
        scope_id: str,
        expected_kind: str | None = None,
    ) -> tuple[StoredArtifact, Path]:
        artifact_id = self._artifact_id(ref)
        artifact = self.store.get_artifact(artifact_id)
        if artifact.scope_id != scope_id:
            raise PermissionError("Artifact belongs to another scope")
        if expected_kind is not None and artifact.kind != expected_kind:
            raise ArtifactRepositoryError("Artifact kind is not accepted by this command")
        metadata = dict(artifact.metadata)
        candidate = self.root / artifact.content
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactRepositoryError("Artifact path is a symbolic link")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ArtifactRepositoryError("Artifact path escapes the repository")
        expected_hash = self._metadata_string(metadata, "sha256")
        data_hash, size = self._inspect(
            path,
            max_bytes=int(metadata.get("size_bytes", 0)) or 1,
        )
        if data_hash != expected_hash or size != int(metadata.get("size_bytes", -1)):
            raise ArtifactRepositoryError("Artifact content no longer matches its record")
        stored = StoredArtifact(
            artifact.id,
            ref,
            artifact.run_id,
            artifact.scope_id,
            artifact.kind,
            self._metadata_string(metadata, "schema_version"),
            self._metadata_string(metadata, "filename"),
            self._metadata_string(metadata, "media_type"),
            data_hash,
            size,
            artifact.content,
            metadata.get("provenance", {}) if isinstance(metadata.get("provenance"), dict) else {},
        )
        return stored, path

    @staticmethod
    def _artifact_id(ref: str) -> str:
        prefix = "artifact://"
        artifact_id = ref.removeprefix(prefix)
        if not ref.startswith(prefix) or len(artifact_id) != 32:
            raise ArtifactRepositoryError("invalid Artifact ref")
        try:
            int(artifact_id, 16)
        except ValueError as error:
            raise ArtifactRepositoryError("invalid Artifact ref") from error
        return artifact_id

    @staticmethod
    def _looks_like_artifact_directory(shard: Path, candidate: Path) -> bool:
        if len(shard.name) != 2 or len(candidate.name) != 32:
            return False
        if candidate.name[:2] != shard.name:
            return False
        try:
            int(candidate.name, 16)
        except ValueError:
            return False
        return True

    @staticmethod
    def _valid_artifact_id(artifact_id: str) -> bool:
        if len(artifact_id) != 32:
            return False
        try:
            int(artifact_id, 16)
        except ValueError:
            return False
        return True

    @staticmethod
    def _inspect(path: Path, *, max_bytes: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise ArtifactRepositoryError("Artifact must be a regular non-symlink file") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactRepositoryError("Artifact must be a regular file")
            if info.st_size < 1 or info.st_size > max_bytes:
                raise ArtifactRepositoryError(
                    "Artifact size is outside the Execution Profile limit"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        finally:
            os.close(descriptor)
        return digest.hexdigest(), info.st_size

    @staticmethod
    def _copy(source: Path, destination: Path) -> None:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
            )
            try:
                with (
                    os.fdopen(source_descriptor, "rb", closefd=False) as reader,
                    os.fdopen(destination_descriptor, "wb", closefd=False) as writer,
                ):
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(source_descriptor)

    @staticmethod
    def _metadata_string(metadata: dict, key: str) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ArtifactRepositoryError(f"Artifact metadata is missing {key}")
        return value
