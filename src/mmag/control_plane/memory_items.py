"""Governed personal memory records and their source lineage."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import TYPE_CHECKING

from ..infrastructure.sqlite.fts import cjk_tokenize_for_fts
from .context import MattermostScopeResolver
from .models import MemoryItem, MemoryItemKind, MemoryItemStatus, ScopeKind

if TYPE_CHECKING:
    import sqlite3
    import threading
    from collections.abc import Iterable

_MEMORY_REF_PREFIX = "memory://"
_MAX_ACTIVE_PER_OWNER = 500


class MemoryItemStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def remember(
        self,
        *,
        installation_id: str,
        tenant_id: str,
        owner_id: str,
        scope_id: str,
        kind: MemoryItemKind,
        content: str,
        source_refs: Iterable[tuple[str, str]] = (),
        confidence: float = 1.0,
        retention_seconds: int = 365 * 24 * 60 * 60,
    ) -> MemoryItem:
        self._validate_identity(installation_id, tenant_id, owner_id, scope_id)
        clean = " ".join(content.split()).strip()[:4_000]
        if not clean:
            raise ValueError("memory content is required")
        if not 0 <= confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        if retention_seconds < 3600 or retention_seconds > 5 * 365 * 24 * 60 * 60:
            raise ValueError("memory retention is outside the supported range")
        sources = tuple(dict.fromkeys(
            (str(source_type)[:40], str(source_ref)[:256])
            for source_type, source_ref in source_refs
            if source_type and source_ref
        ))[:20]
        digest = hashlib.sha256(clean.casefold().encode()).hexdigest()
        now = time.time()
        with self.lock:
            self._expire_locked(owner_id, now)
            existing = self.connection.execute(
                """SELECT id FROM memory_items WHERE installation_id=? AND tenant_id=?
                AND owner_id=? AND content_hash=? AND status='active' LIMIT 1""",
                (installation_id, tenant_id, owner_id, digest),
            ).fetchone()
            if existing is not None:
                return self.get(str(existing["id"]), owner_id=owner_id)
            count = self.connection.execute(
                """SELECT COUNT(*) FROM memory_items WHERE installation_id=?
                AND tenant_id=? AND owner_id=? AND status='active'""",
                (installation_id, tenant_id, owner_id),
            ).fetchone()[0]
            if int(count) >= _MAX_ACTIVE_PER_OWNER:
                raise ValueError("personal memory quota is exhausted")
            identifier = uuid.uuid4().hex
            self.connection.execute(
                """INSERT INTO memory_items
                (id, installation_id, tenant_id, owner_id, scope_id, kind, content,
                 content_hash, confidence, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (identifier, installation_id, tenant_id, owner_id, scope_id, kind.value,
                 clean, digest, confidence, now + retention_seconds, now, now),
            )
            self.connection.execute(
                "INSERT INTO memory_items_fts(content) VALUES (?)",
                (cjk_tokenize_for_fts(clean),),
            )
            rowid = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            self.connection.execute(
                "INSERT INTO memory_items_fts_map(rowid, memory_id) VALUES (?, ?)",
                (rowid, identifier),
            )
            for source_type, source_ref in sources:
                self.connection.execute(
                    """INSERT INTO memory_item_sources
                    (memory_id, source_type, source_ref, source_scope_id)
                    VALUES (?, ?, ?, ?)""",
                    (identifier, source_type, source_ref, scope_id),
                )
            self.connection.commit()
        return self.get(identifier, owner_id=owner_id)

    def get(self, ref_or_id: str, *, owner_id: str) -> MemoryItem:
        identifier = self._id(ref_or_id)
        self.expire(owner_id=owner_id)
        row = self.connection.execute(
            "SELECT * FROM memory_items WHERE id=? AND owner_id=?",
            (identifier, owner_id),
        ).fetchone()
        if row is None:
            raise KeyError(ref_or_id)
        return self._record(row)

    def list_active(
        self, *, installation_id: str, tenant_id: str, owner_id: str, limit: int = 20
    ) -> tuple[MemoryItem, ...]:
        self.expire(owner_id=owner_id)
        rows = self.connection.execute(
            """SELECT * FROM memory_items WHERE installation_id=? AND tenant_id=?
            AND owner_id=? AND status='active' ORDER BY updated_at DESC LIMIT ?""",
            (installation_id, tenant_id, owner_id, max(1, min(limit, 100))),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def search(
        self,
        query: str,
        *,
        installation_id: str,
        tenant_id: str,
        owner_id: str,
        limit: int = 5,
    ) -> tuple[MemoryItem, ...]:
        self.expire(owner_id=owner_id)
        query = query.strip()[:1_000]
        if not query:
            return self.list_active(
                installation_id=installation_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                limit=limit,
            )
        rows = self.connection.execute(
            """SELECT item.* FROM memory_items_fts AS fts
            JOIN memory_items_fts_map AS map ON map.rowid=fts.rowid
            JOIN memory_items AS item ON item.id=map.memory_id
            WHERE item.installation_id=? AND item.tenant_id=? AND item.owner_id=?
              AND item.status='active' AND memory_items_fts MATCH ?
            ORDER BY bm25(memory_items_fts), item.updated_at DESC LIMIT ?""",
            (installation_id, tenant_id, owner_id, self._match_expression(query),
             max(1, min(limit, 20))),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def revoke(self, ref_or_id: str, *, owner_id: str) -> MemoryItem:
        identifier = self._id(ref_or_id)
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE memory_items SET status='revoked', updated_at=?
                WHERE id=? AND owner_id=? AND status IN ('active', 'proposed')""",
                (time.time(), identifier, owner_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(ref_or_id)
            self.connection.commit()
        return self.get(identifier, owner_id=owner_id)

    def revoke_source(
        self, source_type: str, source_ref: str, *, installation_id: str, tenant_id: str
    ) -> int:
        with self.lock:
            cursor = self.connection.execute(
                """UPDATE memory_items SET status='revoked', updated_at=?
                WHERE installation_id=? AND tenant_id=?
                AND status IN ('active', 'proposed') AND id IN (
                    SELECT memory_id FROM memory_item_sources
                    WHERE source_type=? AND source_ref=?
                )""",
                (time.time(), installation_id, tenant_id, source_type, source_ref),
            )
            self.connection.commit()
        return int(cursor.rowcount)

    def expire(self, *, owner_id: str) -> int:
        with self.lock:
            count = self._expire_locked(owner_id, time.time())
            self.connection.commit()
        return count

    def _expire_locked(self, owner_id: str, now: float) -> int:
        cursor = self.connection.execute(
            """UPDATE memory_items SET status='expired', updated_at=?
            WHERE owner_id=? AND status='active' AND expires_at>0 AND expires_at<=?""",
            (now, owner_id, now),
        )
        return int(cursor.rowcount)

    def _record(self, row) -> MemoryItem:
        sources = self.connection.execute(
            """SELECT source_type, source_ref FROM memory_item_sources
            WHERE memory_id=? ORDER BY source_type, source_ref""",
            (row["id"],),
        ).fetchall()
        return MemoryItem(
            id=row["id"], installation_id=row["installation_id"],
            tenant_id=row["tenant_id"], owner_id=row["owner_id"],
            scope_id=row["scope_id"], kind=MemoryItemKind(row["kind"]),
            content=row["content"], content_hash=row["content_hash"],
            source_refs=tuple(f"{item[0]}://{item[1]}" for item in sources),
            confidence=float(row["confidence"]), status=MemoryItemStatus(row["status"]),
            expires_at=float(row["expires_at"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _id(ref_or_id: str) -> str:
        identifier = ref_or_id.removeprefix(_MEMORY_REF_PREFIX)
        if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise ValueError("invalid memory ref")
        return identifier

    @staticmethod
    def _match_expression(query: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9_]+|[一-鿿㐀-䶿]", query)[:40]
        if not tokens:
            return '""'
        return " OR ".join(f'"{token}"' for token in tokens)

    @staticmethod
    def _validate_identity(
        installation_id: str, tenant_id: str, owner_id: str, scope_id: str
    ) -> None:
        parsed_installation, parsed_tenant, kind, resource_id = MattermostScopeResolver.parse(
            scope_id
        )
        if (kind is not ScopeKind.PERSONAL or parsed_installation != installation_id
                or parsed_tenant != tenant_id or resource_id != owner_id):
            raise PermissionError("memory identity does not match its Personal Scope")
