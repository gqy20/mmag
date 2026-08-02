"""Tenant-scoped immutable revisions for user-authored workflow overlays."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import TYPE_CHECKING

from .context import MattermostScopeResolver
from .models import PersonalSkill, PersonalSkillStatus, ScopeKind

if TYPE_CHECKING:
    import sqlite3
    import threading
    from collections.abc import Iterable

_PACKAGE_REF = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}@[0-9]+\.[0-9]+\.[0-9]+$")
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PERSONAL_REF = re.compile(r"^pskill://([a-f0-9]{32})@([1-9][0-9]*)$")


class PersonalSkillStore:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def create_revision(
        self,
        *,
        installation_id: str,
        tenant_id: str,
        owner_id: str,
        scope_id: str,
        name: str,
        base_skill_ref: str,
        instruction: str,
        description: str = "",
        preferred_agent: str = "",
        activation_intents: Iterable[str] = (),
        activation_keywords: Iterable[str] = (),
        auto_select: bool = False,
        template: str = "",
        skill_id: str = "",
        source_case_ids: Iterable[str] = (),
    ) -> PersonalSkill:
        self._validate_identity(installation_id, tenant_id, owner_id, scope_id)
        name = self._bounded_text(name, "name", 80, required=True)
        description = self._bounded_text(description, "description", 500)
        instruction = self._bounded_text(instruction, "instruction", 12_000, required=True)
        template = self._bounded_text(template, "template", 20_000)
        if not _PACKAGE_REF.fullmatch(base_skill_ref):
            raise ValueError("personal Skill requires an exact base Skill ref")
        if preferred_agent and not _AGENT_NAME.fullmatch(preferred_agent):
            raise ValueError("personal Skill preferred Agent is invalid")
        intents = self._terms(activation_intents, "activation intent")
        keywords = self._terms(activation_keywords, "activation keyword")
        case_ids = tuple(dict.fromkeys(str(item) for item in source_case_ids if str(item)))
        if len(case_ids) > 20 or any(not re.fullmatch(r"[a-f0-9]{32}", item) for item in case_ids):
            raise ValueError("personal Skill source WorkCase IDs are invalid")
        identifier = skill_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("personal Skill id is invalid")
        with self.lock:
            row = self.connection.execute(
                "SELECT MAX(revision) FROM personal_skills WHERE id=?", (identifier,)
            ).fetchone()
            revision = int(row[0] or 0) + 1
            digest = self._hash(
                identifier,
                revision,
                owner_id,
                scope_id,
                name,
                description,
                base_skill_ref,
                preferred_agent,
                intents,
                keywords,
                auto_select,
                instruction,
                template,
                case_ids,
            )
            now = time.time()
            self.connection.execute(
                """INSERT INTO personal_skills
                (id, revision, installation_id, tenant_id, owner_id, scope_id,
                 name, description, base_skill_ref, preferred_agent,
                 activation_intents, activation_keywords, auto_select,
                 instruction, template, sha256, source_case_ids, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                (
                    identifier,
                    revision,
                    installation_id,
                    tenant_id,
                    owner_id,
                    scope_id,
                    name,
                    description,
                    base_skill_ref,
                    preferred_agent,
                    json.dumps(intents, ensure_ascii=False),
                    json.dumps(keywords, ensure_ascii=False),
                    int(auto_select),
                    instruction,
                    template,
                    digest,
                    json.dumps(case_ids, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self.connection.commit()
        return self.get(f"pskill://{identifier}@{revision}", owner_id=owner_id)

    def activate(self, ref: str, *, owner_id: str) -> PersonalSkill:
        skill_id, revision = self._parse_ref(ref)
        with self.lock:
            selected = self.get(ref, owner_id=owner_id)
            self.connection.execute(
                """UPDATE personal_skills SET status='archived', updated_at=?
                WHERE id=? AND owner_id=? AND status='active'""",
                (time.time(), skill_id, owner_id),
            )
            self.connection.execute(
                """UPDATE personal_skills SET status='active', updated_at=?
                WHERE id=? AND revision=? AND owner_id=?""",
                (time.time(), skill_id, revision, owner_id),
            )
            self.connection.commit()
        return self.get(selected.ref, owner_id=owner_id)

    def archive(self, ref: str, *, owner_id: str) -> PersonalSkill:
        skill_id, revision = self._parse_ref(ref)
        with self.lock:
            self.get(ref, owner_id=owner_id)
            self.connection.execute(
                """UPDATE personal_skills SET status='archived', updated_at=?
                WHERE id=? AND revision=? AND owner_id=?""",
                (time.time(), skill_id, revision, owner_id),
            )
            self.connection.commit()
        return self.get(ref, owner_id=owner_id)

    def get(self, ref: str, *, owner_id: str) -> PersonalSkill:
        skill_id, revision = self._parse_ref(ref)
        row = self.connection.execute(
            """SELECT * FROM personal_skills
            WHERE id=? AND revision=? AND owner_id=?""",
            (skill_id, revision, owner_id),
        ).fetchone()
        if row is None:
            raise KeyError(ref)
        return self._record(row)

    def list_active(
        self, *, installation_id: str, tenant_id: str, owner_id: str
    ) -> tuple[PersonalSkill, ...]:
        rows = self.connection.execute(
            """SELECT * FROM personal_skills
            WHERE installation_id=? AND tenant_id=? AND owner_id=? AND status='active'
            ORDER BY updated_at DESC""",
            (installation_id, tenant_id, owner_id),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def list_latest(
        self, *, installation_id: str, tenant_id: str, owner_id: str
    ) -> tuple[PersonalSkill, ...]:
        rows = self.connection.execute(
            """SELECT current.* FROM personal_skills current
            JOIN (SELECT id, MAX(revision) revision FROM personal_skills
                  WHERE installation_id=? AND tenant_id=? AND owner_id=? GROUP BY id) latest
              ON current.id=latest.id AND current.revision=latest.revision
            ORDER BY current.updated_at DESC""",
            (installation_id, tenant_id, owner_id),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def list_revisions(self, skill_id: str, *, owner_id: str) -> tuple[PersonalSkill, ...]:
        rows = self.connection.execute(
            "SELECT * FROM personal_skills WHERE id=? AND owner_id=? ORDER BY revision DESC",
            (skill_id, owner_id),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _validate_identity(
        installation_id: str, tenant_id: str, owner_id: str, scope_id: str
    ) -> None:
        try:
            parsed_installation, parsed_tenant, kind, resource_id = (
                MattermostScopeResolver.parse(scope_id)
            )
        except ValueError as error:
            raise ValueError("personal Skill scope is invalid") from error
        if (
            kind is not ScopeKind.PERSONAL
            or parsed_installation != installation_id
            or parsed_tenant != tenant_id
            or resource_id != owner_id
        ):
            raise PermissionError("personal Skill identity does not match its scope")

    @staticmethod
    def _bounded_text(value: str, label: str, limit: int, *, required: bool = False) -> str:
        normalized = value.strip()
        if required and not normalized:
            raise ValueError(f"personal Skill {label} is required")
        if len(normalized) > limit or "\x00" in normalized:
            raise ValueError(f"personal Skill {label} is invalid")
        return normalized

    @classmethod
    def _terms(cls, values: Iterable[str], label: str) -> tuple[str, ...]:
        terms = tuple(dict.fromkeys(cls._bounded_text(str(item), label, 80) for item in values))
        if len(terms) > 20:
            raise ValueError(f"personal Skill {label}s exceed the limit")
        return terms

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, int]:
        match = _PERSONAL_REF.fullmatch(ref)
        if match is None:
            raise ValueError("personal Skill ref is invalid")
        return match.group(1), int(match.group(2))

    @staticmethod
    def _hash(*values: object) -> str:
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _record(row) -> PersonalSkill:
        return PersonalSkill(
            id=row["id"],
            revision=row["revision"],
            installation_id=row["installation_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            scope_id=row["scope_id"],
            name=row["name"],
            description=row["description"],
            base_skill_ref=row["base_skill_ref"],
            preferred_agent=row["preferred_agent"],
            activation_intents=tuple(json.loads(row["activation_intents"])),
            activation_keywords=tuple(json.loads(row["activation_keywords"])),
            auto_select=bool(row["auto_select"]),
            instruction=row["instruction"],
            template=row["template"],
            sha256=row["sha256"],
            source_case_ids=tuple(json.loads(row["source_case_ids"])),
            status=PersonalSkillStatus(row["status"]),
        )
