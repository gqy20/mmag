"""Small forward-only migration runner for SQLite."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...logger import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

log = get_logger(__name__)

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at REAL NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A schema migration could not be validated or applied."""


class FutureSchemaError(MigrationError):
    """The database was created by a newer version of mmag."""


@dataclass(frozen=True)
class Migration:
    """One immutable, forward-only schema migration."""

    version: int
    name: str
    checksum: str
    upgrade: Callable[[sqlite3.Connection], None]


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> None:
    """Validate migration history and atomically apply pending migrations."""
    if migrations is None:
        from .migrations import DEFAULT_MIGRATIONS

        migrations = DEFAULT_MIGRATIONS

    ordered = _validate_definitions(migrations)
    connection.execute(_MIGRATION_TABLE_SQL)
    connection.commit()

    applied_rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = {int(row[0]): (str(row[1]), str(row[2])) for row in applied_rows}
    supported = {migration.version: migration for migration in ordered}
    latest_supported = ordered[-1].version if ordered else 0

    future_versions = sorted(version for version in applied if version > latest_supported)
    if future_versions:
        raise FutureSchemaError(
            f"database schema {future_versions[-1]} is newer than supported {latest_supported}"
        )

    _validate_history(applied, supported)

    for migration in ordered:
        if migration.version in applied:
            continue
        _apply_one(connection, migration)


def _validate_definitions(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions = [migration.version for migration in ordered]
    expected = list(range(1, len(ordered) + 1))
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 1; got {versions}, expected {expected}"
        )
    for migration in ordered:
        if not migration.name.strip() or not migration.checksum.strip():
            raise MigrationError(f"migration {migration.version} requires name and checksum")
    return ordered


def _validate_history(
    applied: dict[int, tuple[str, str]],
    supported: dict[int, Migration],
) -> None:
    if applied:
        expected = set(range(1, max(applied) + 1))
        missing = sorted(expected - set(applied))
        if missing:
            raise MigrationError(f"database migration history has gaps: {missing}")

    for version, (name, checksum) in applied.items():
        migration = supported.get(version)
        if migration is None:
            raise FutureSchemaError(f"database migration {version} is not supported by this build")
        if (name, checksum) != (migration.name, migration.checksum):
            raise MigrationError(
                f"migration {version} history mismatch: database has {name!r}/{checksum!r}"
            )


def _apply_one(connection: sqlite3.Connection, migration: Migration) -> None:
    log.info("应用 SQLite migration v%03d: %s", migration.version, migration.name)
    try:
        connection.execute("BEGIN IMMEDIATE")
        migration.upgrade(connection)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (migration.version, migration.name, migration.checksum, time.time()),
        )
        connection.commit()
    except Exception as error:
        connection.rollback()
        raise MigrationError(
            f"migration v{migration.version:03d} {migration.name!r} failed: {error}"
        ) from error
