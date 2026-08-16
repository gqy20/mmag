"""Provider-neutral, bounded knowledge retrieval contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    text: str
    scope_id: str
    limit: int

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.scope_id.strip():
            raise ValueError("knowledge query and scope are required")
        if self.limit < 1 or self.limit > 50:
            raise ValueError("knowledge result limit must be between 1 and 50")


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_system: str
    resource_id: str
    version: str
    title: str
    snippet: str
    updated_at: float
    visible_scope_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        required = (
            self.source_system,
            self.resource_id,
            self.version,
            self.visible_scope_id,
            self.content_sha256,
        )
        if not all(value.strip() for value in required):
            raise ValueError("source provenance is incomplete")
        if len(self.content_sha256) != 64:
            raise ValueError("source content hash must be SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "source_system": self.source_system,
            "resource_id": self.resource_id,
            "version": self.version,
            "title": self.title,
            "snippet": self.snippet,
            "updated_at": self.updated_at,
            "visible_scope_id": self.visible_scope_id,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_content(
        cls,
        *,
        source_system: str,
        resource_id: str,
        version: str,
        title: str,
        snippet: str,
        updated_at: float,
        visible_scope_id: str,
        content: str,
    ) -> SourceRef:
        return cls(
            source_system=source_system,
            resource_id=resource_id,
            version=version,
            title=title,
            snippet=snippet,
            updated_at=updated_at,
            visible_scope_id=visible_scope_id,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeResult:
    items: tuple[dict[str, Any], ...]
    sources: tuple[SourceRef, ...]
    partial: bool = False
    error_code: str = ""

    def __post_init__(self) -> None:
        if len(self.items) != len(self.sources):
            raise ValueError("each knowledge result item requires one SourceRef")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "count": len(self.items),
            "items": list(self.items),
            "sources": [source.to_dict() for source in self.sources],
            "partial": self.partial,
            "error_code": self.error_code,
        }
