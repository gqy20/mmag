"""Request-scoped, budgeted progressive disclosure of Skill resources."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any

from .errors import SkillReferenceError
from .loader import resolve_skill_ref

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from .models import SkillFileAsset, SkillPackage


@dataclass(frozen=True, slots=True)
class LoadedSkillResource:
    ref: str
    kind: str
    sha256: str
    size_bytes: int
    estimated_tokens: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "estimated_tokens": self.estimated_tokens,
        }


class SkillResourceCache:
    """Cache immutable UTF-8 resources by their registered content Hash."""

    def __init__(self) -> None:
        self._content_by_hash: dict[str, str] = {}
        self._lock = RLock()

    def read(self, package: SkillPackage, asset: SkillFileAsset) -> str:
        with self._lock:
            cached = self._content_by_hash.get(asset.sha256)
        if cached is not None:
            return cached
        raw = resolve_skill_ref(package.root, asset.ref).read_bytes()
        if hashlib.sha256(raw).hexdigest() != asset.sha256:
            raise SkillReferenceError(
                f"Skill resource changed after registration: {package.manifest.metadata.ref}/{asset.ref}"
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SkillReferenceError(f"Skill resource {asset.ref!r} is not UTF-8") from error
        with self._lock:
            return self._content_by_hash.setdefault(asset.sha256, content)


class SkillResourceSession:
    """Track exactly which resources one selected Skill disclosed."""

    def __init__(self, package: SkillPackage, cache: SkillResourceCache) -> None:
        self.package = package
        self.cache = cache
        self._loaded: dict[str, LoadedSkillResource] = {}
        self._content: dict[str, str] = {}
        self._lock = RLock()

    @property
    def skill_ref(self) -> str:
        return self.package.manifest.metadata.ref

    def load(self, ref: str) -> dict[str, Any]:
        with self._lock:
            kind = self._resource_kind(ref)
            record = self._loaded.get(ref)
            if record is None:
                record = self._load_new(ref, kind)
            return {
                **record.to_dict(),
                "content": self._content[ref],
            }

    def _load_new(self, ref: str, kind: str) -> LoadedSkillResource:
        asset = self.package.resources[ref]
        content = self.cache.read(self.package, asset)
        tokens = max(1, (len(content.encode("utf-8")) + 2) // 3)
        record = LoadedSkillResource(ref, kind, asset.sha256, asset.size_bytes, tokens)
        self._check_budget(record)
        self._loaded[ref] = record
        self._content[ref] = content
        return record

    def _resource_kind(self, ref: str) -> str:
        resources = self.package.manifest.resources
        if ref in resources.templates:
            return "template"
        if ref in resources.references:
            return "reference"
        raise SkillReferenceError(f"Skill resource {ref!r} is not declared")

    def _check_budget(self, candidate: LoadedSkillResource) -> None:
        disclosure = self.package.manifest.disclosure
        if candidate.size_bytes > disclosure.max_resource_bytes:
            raise SkillReferenceError(f"Skill resource {candidate.ref!r} exceeds its byte limit")
        if len(self._loaded) + 1 > disclosure.max_resources:
            raise SkillReferenceError("Skill resource count budget exceeded")
        if self.total_bytes + candidate.size_bytes > disclosure.max_total_bytes:
            raise SkillReferenceError("Skill resource byte budget exceeded")
        if self.estimated_tokens + candidate.estimated_tokens > disclosure.max_estimated_tokens:
            raise SkillReferenceError("Skill resource token budget exceeded")

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return sum(item.size_bytes for item in self._loaded.values())

    @property
    def estimated_tokens(self) -> int:
        with self._lock:
            return sum(item.estimated_tokens for item in self._loaded.values())

    def provenance(self) -> dict[str, str]:
        with self._lock:
            resources = [self._loaded[ref].to_dict() for ref in sorted(self._loaded)]
            digest = hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()
            return {
                "skill_resource_count": str(len(resources)),
                "skill_resource_bytes": str(self.total_bytes),
                "skill_resource_estimated_tokens": str(self.estimated_tokens),
                "skill_resource_hash": digest,
                "skill_resources_json": json.dumps(
                    resources, sort_keys=True, separators=(",", ":")
                ),
            }

    def to_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "skill_ref": self.skill_ref,
                "loaded_refs": sorted(self._loaded),
            }


class SkillResourceLoader:
    def __init__(self, cache: SkillResourceCache | None = None) -> None:
        self.cache = cache or SkillResourceCache()

    def create_session(self, package: SkillPackage) -> SkillResourceSession:
        return SkillResourceSession(package, self.cache)

    def restore_session(
        self,
        package: SkillPackage,
        state: Mapping[str, Any],
    ) -> SkillResourceSession:
        if state.get("skill_ref") != package.manifest.metadata.ref:
            raise SkillReferenceError("Skill resource state does not match its Package")
        refs = state.get("loaded_refs", ())
        if not isinstance(refs, (list, tuple)) or not all(isinstance(ref, str) for ref in refs):
            raise SkillReferenceError("Skill resource state contains invalid refs")
        session = self.create_session(package)
        for ref in refs:
            session.load(ref)
        return session


def build_skill_resource_catalog(package: SkillPackage) -> str:
    lines: list[str] = []
    for kind, refs in (
        ("template", package.manifest.resources.templates),
        ("reference", package.manifest.resources.references),
    ):
        lines.extend(
            f"- `{ref}` ({kind}, {package.resources[ref].size_bytes} bytes)" for ref in refs
        )
    if not lines:
        return ""
    return (
        "## Available Skill Resources\n"
        "Load only the specific resource needed for the current step with "
        "`load_skill_resource`.\n"
        + "\n".join(lines)
    )


_CURRENT_SESSION: ContextVar[SkillResourceSession | None] = ContextVar(
    "mmag_skill_resource_session",
    default=None,
)


def get_skill_resource_session() -> SkillResourceSession | None:
    return _CURRENT_SESSION.get()


@contextmanager
def bind_skill_resource_session(session: SkillResourceSession) -> Iterator[None]:
    token = _CURRENT_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_SESSION.reset(token)


def load_active_skill_resource(ref: str) -> dict[str, Any]:
    session = get_skill_resource_session()
    if session is None:
        raise SkillReferenceError("no active Skill resource session")
    return session.load(ref)
