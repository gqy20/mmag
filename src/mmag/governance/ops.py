"""Small operational primitives for observability and recoverability."""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections import defaultdict
from pathlib import Path


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def increment(self, name: str, **labels: str) -> None:
        self._counters[(name, tuple(sorted(labels.items())))] += 1

    def value(self, name: str, **labels: str) -> int:
        return self._counters[(name, tuple(sorted(labels.items())))]


def backup_sqlite(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source_path)
    try:
        target_connection = sqlite3.connect(destination_path)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    return destination_path


def purge_expired_rows(connection: sqlite3.Connection, *, before: float | None = None) -> int:
    cutoff = before if before is not None else time.time()
    cursor = connection.execute(
        "DELETE FROM url_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (cutoff,)
    )
    connection.commit()
    return cursor.rowcount


def atomic_copy(source: str | Path, destination: str | Path) -> Path:
    """Copy non-database deployment artifacts without partial destination writes."""
    source_path = Path(source)
    destination_path = Path(destination)
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    shutil.copy2(source_path, temporary)
    temporary.replace(destination_path)
    return destination_path
