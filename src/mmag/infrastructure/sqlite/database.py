"""SQLite connection lifecycle."""

import sqlite3
from pathlib import Path

from ...logger import get_logger
from .migration import apply_migrations

log = get_logger(__name__)


class SQLiteDatabase:
    """Open a configured SQLite connection and migrate it before use."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000):
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            uri=self.path.startswith("file:"),
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            apply_migrations(connection)
        except Exception:
            connection.close()
            raise

        log.info("SQLite 数据库就绪: %s", self.path)
        return connection
