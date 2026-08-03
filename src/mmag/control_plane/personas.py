"""Immutable Digital Persona revisions backed by explicit published snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from .context import MattermostScopeResolver
from .models import DigitalPersona, DigitalPersonaStatus, MemoryItemStatus, ScopeKind

if TYPE_CHECKING:
    import sqlite3
    import threading
    from collections.abc import Iterable

_REF = re.compile(r"^persona://([a-f0-9]{32})@([1-9][0-9]*)$")


class DigitalPersonaStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create_revision(
        self,
        *,
        installation_id: str,
        tenant_id: str,
        owner_id: str,
        owner_username: str,
        scope_id: str,
        display_name: str,
        allowed_topics: Iterable[str] = (),
        approval_topics: Iterable[str] = (),
        denied_topics: Iterable[str] = (),
        response_mode: str = "risk_approval",
        source_memory_ids: Iterable[str] = (),
        persona_id: str = "",
    ) -> DigitalPersona:
        self._validate_identity(installation_id, tenant_id, owner_id, scope_id)
        display_name = " ".join(display_name.split()).strip()[:80]
        owner_username = owner_username.strip().lstrip("@").lower()[:64]
        if not display_name or not owner_username:
            raise ValueError("persona display name and owner username are required")
        if response_mode not in {"auto", "risk_approval", "owner_approval"}:
            raise ValueError("unsupported persona response mode")
        allowed = self._terms(allowed_topics)
        approval = self._terms(approval_topics)
        denied = self._terms(denied_topics)
        memory_ids = tuple(dict.fromkeys(
            str(item).removeprefix("memory://") for item in source_memory_ids if str(item)
        ))[:20]
        snapshots = self._snapshots(
            memory_ids,
            installation_id=installation_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        identifier = persona_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("invalid persona id")
        with self.lock:
            latest = self.connection.execute(
                "SELECT MAX(revision) FROM digital_personas WHERE id=?", (identifier,)
            ).fetchone()
            revision = int(latest[0] or 0) + 1
            payload = {
                "id": identifier, "revision": revision, "owner_id": owner_id,
                "display_name": display_name, "allowed_topics": allowed,
                "approval_topics": approval, "denied_topics": denied,
                "response_mode": response_mode,
                "source_memory_ids": memory_ids, "published_snapshots": snapshots,
            }
            digest = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            now = time.time()
            self.connection.execute(
                """INSERT INTO digital_personas
                (id, revision, installation_id, tenant_id, owner_id, owner_username,
                 scope_id, display_name, allowed_topics, approval_topics, denied_topics,
                 response_mode,
                 source_memory_ids, published_snapshots, sha256, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                (identifier, revision, installation_id, tenant_id, owner_id,
                 owner_username, scope_id, display_name,
                 json.dumps(allowed, ensure_ascii=False),
                 json.dumps(approval, ensure_ascii=False),
                 json.dumps(denied, ensure_ascii=False), response_mode,
                 json.dumps(memory_ids, ensure_ascii=False),
                 json.dumps(snapshots, ensure_ascii=False), digest, now, now),
            )
            self.connection.commit()
        return self.get(f"persona://{identifier}@{revision}", owner_id=owner_id)

    def activate(self, ref: str, *, owner_id: str) -> DigitalPersona:
        persona = self.get(ref, owner_id=owner_id)
        if not persona.published_snapshots:
            raise ValueError("publish at least one personal memory before activation")
        with self.lock:
            self.connection.execute(
                """UPDATE digital_personas SET status='archived', updated_at=?
                WHERE id=? AND owner_id=? AND status='active'""",
                (time.time(), persona.id, owner_id),
            )
            self.connection.execute(
                """UPDATE digital_personas SET status='active', updated_at=?
                WHERE id=? AND revision=? AND owner_id=?""",
                (time.time(), persona.id, persona.revision, owner_id),
            )
            self.connection.commit()
        return self.get(ref, owner_id=owner_id)

    def archive(self, ref: str, *, owner_id: str) -> DigitalPersona:
        persona = self.get(ref, owner_id=owner_id)
        with self.lock:
            self.connection.execute(
                """UPDATE digital_personas SET status='archived', updated_at=?
                WHERE id=? AND revision=? AND owner_id=?""",
                (time.time(), persona.id, persona.revision, owner_id),
            )
            self.connection.commit()
        return self.get(ref, owner_id=owner_id)

    def archive_by_memory(self, memory_id: str, *, owner_id: str = "") -> int:
        memory_id = memory_id.removeprefix("memory://")
        clauses = [
            "status='active'",
            "EXISTS (SELECT 1 FROM json_each(source_memory_ids) WHERE value=?)",
        ]
        params: list[Any] = [memory_id]
        if owner_id:
            clauses.append("owner_id=?")
            params.append(owner_id)
        with self.lock:
            cursor = self.connection.execute(
                f"UPDATE digital_personas SET status='archived', updated_at=? "
                f"WHERE {' AND '.join(clauses)}",
                (time.time(), *params),
            )
            self.connection.commit()
        return int(cursor.rowcount)

    def get(self, ref: str, *, owner_id: str = "") -> DigitalPersona:
        identifier, revision = self._parse(ref)
        query = "SELECT * FROM digital_personas WHERE id=? AND revision=?"
        params: tuple[Any, ...] = (identifier, revision)
        if owner_id:
            query += " AND owner_id=?"
            params = (*params, owner_id)
        row = self.connection.execute(query, params).fetchone()
        if row is None:
            raise KeyError(ref)
        return self._record(row)

    def list_latest_owner(
        self, *, installation_id: str, tenant_id: str, owner_id: str
    ) -> tuple[DigitalPersona, ...]:
        rows = self.connection.execute(
            """SELECT current.* FROM digital_personas current
            JOIN (SELECT id, MAX(revision) revision FROM digital_personas
                  WHERE installation_id=? AND tenant_id=? AND owner_id=? GROUP BY id) latest
              ON current.id=latest.id AND current.revision=latest.revision
            ORDER BY current.updated_at DESC""",
            (installation_id, tenant_id, owner_id),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def find_active(
        self, target: str, *, installation_id: str, tenant_id: str
    ) -> tuple[DigitalPersona, ...]:
        normalized = self._normalize(target)
        rows = self.connection.execute(
            """SELECT * FROM digital_personas WHERE installation_id=? AND tenant_id=?
            AND status='active' ORDER BY updated_at DESC""",
            (installation_id, tenant_id),
        ).fetchall()
        matches = []
        for row in rows:
            persona = self._record(row)
            aliases = {
                self._normalize(persona.display_name),
                self._normalize(persona.owner_username),
                self._normalize(persona.display_name.removesuffix("的数字人")),
            }
            if normalized in aliases:
                matches.append(persona)
        return tuple(matches)

    def _snapshots(
        self, memory_ids: tuple[str, ...], *, installation_id: str, tenant_id: str,
        owner_id: str,
    ) -> tuple[dict[str, Any], ...]:
        snapshots = []
        for memory_id in memory_ids:
            row = self.connection.execute(
                """SELECT id, kind, content, content_hash, status FROM memory_items
                WHERE id=? AND installation_id=? AND tenant_id=? AND owner_id=?""",
                (memory_id, installation_id, tenant_id, owner_id),
            ).fetchone()
            if row is None or row["status"] != MemoryItemStatus.ACTIVE.value:
                raise ValueError("persona source memory is unavailable")
            snapshots.append({
                "memory_ref": f"memory://{row['id']}", "kind": row["kind"],
                "content": str(row["content"])[:4_000], "content_hash": row["content_hash"],
            })
        return tuple(snapshots)

    @staticmethod
    def _record(row) -> DigitalPersona:
        return DigitalPersona(
            id=row["id"], revision=int(row["revision"]),
            installation_id=row["installation_id"], tenant_id=row["tenant_id"],
            owner_id=row["owner_id"], owner_username=row["owner_username"],
            scope_id=row["scope_id"], display_name=row["display_name"],
            allowed_topics=tuple(json.loads(row["allowed_topics"])),
            approval_topics=tuple(json.loads(row["approval_topics"])),
            denied_topics=tuple(json.loads(row["denied_topics"])),
            response_mode=row["response_mode"],
            source_memory_ids=tuple(json.loads(row["source_memory_ids"])),
            published_snapshots=tuple(json.loads(row["published_snapshots"])),
            sha256=row["sha256"], status=DigitalPersonaStatus(row["status"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _terms(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            " ".join(str(value).split()).strip()[:80]
            for value in values if str(value).strip()
        ))[:30]

    @staticmethod
    def _parse(ref: str) -> tuple[str, int]:
        match = _REF.fullmatch(ref)
        if match is None:
            raise ValueError("invalid persona ref")
        return match.group(1), int(match.group(2))

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s@·._-]+", "", value).lower()

    @staticmethod
    def _validate_identity(
        installation_id: str, tenant_id: str, owner_id: str, scope_id: str
    ) -> None:
        parsed_installation, parsed_tenant, kind, resource_id = MattermostScopeResolver.parse(
            scope_id
        )
        if (kind is not ScopeKind.PERSONAL or parsed_installation != installation_id
                or parsed_tenant != tenant_id or resource_id != owner_id):
            raise PermissionError("persona identity does not match its Personal Scope")
