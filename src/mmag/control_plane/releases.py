"""Atomic package release records for the local control plane."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


class ReleaseStore:
    def __init__(self, connection: Any, lock: Any) -> None:
        self._connection = connection
        self._lock = lock

    def activate(self, records: tuple[dict[str, Any], ...]) -> None:
        """Publish a fully validated package batch or publish nothing."""
        now = time.time()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for record in records:
                    self._connection.execute(
                        """UPDATE package_releases SET status='superseded', updated_at=?
                        WHERE package_kind=? AND package_name=? AND status='active'
                          AND package_hash<>?""",
                        (
                            now,
                            record["package_kind"],
                            record["package_name"],
                            record["package_hash"],
                        ),
                    )
                    self._connection.execute(
                        """INSERT INTO package_releases
                        (id, package_kind, package_name, package_version, package_hash,
                         eval_hash, gate_version, checks, status,
                         released_by, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                        ON CONFLICT(package_kind, package_hash) DO UPDATE SET
                        status='active', checks=excluded.checks,
                        released_by=excluded.released_by, updated_at=excluded.updated_at""",
                        (
                            uuid.uuid4().hex,
                            record["package_kind"],
                            record["package_name"],
                            record["package_version"],
                            record["package_hash"],
                            record["eval_hash"],
                            record["gate_version"],
                            json.dumps(record["checks"], separators=(",", ":")),
                            record["released_by"],
                            now,
                            now,
                        ),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def list_active(self, package_kind: str = "") -> list[dict[str, Any]]:
        query = "SELECT * FROM package_releases WHERE status='active'"
        parameters: tuple[str, ...] = ()
        if package_kind:
            query += " AND package_kind=?"
            parameters = (package_kind,)
        rows = self._connection.execute(
            query + " ORDER BY package_kind, package_name", parameters
        ).fetchall()
        return [
            {
                **dict(row),
                "checks": json.loads(row["checks"]),
            }
            for row in rows
        ]
