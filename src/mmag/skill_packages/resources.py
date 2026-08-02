"""Request-scoped identity for the governed Skill selected by MMAG."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .models import SkillPackage


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Expose the validated active package to capabilities and execution policy."""

    package: SkillPackage
    personal_ref: str = ""
    personal_hash: str = ""

    @property
    def skill_ref(self) -> str:
        return self.package.manifest.metadata.ref

    def to_state(self) -> dict[str, str]:
        return {
            "skill_ref": self.skill_ref,
            "personal_skill_ref": self.personal_ref,
            "personal_skill_hash": self.personal_hash,
        }


_CURRENT_SKILL: ContextVar[SkillContext | None] = ContextVar(
    "mmag_skill_context",
    default=None,
)


def get_skill_context() -> SkillContext | None:
    return _CURRENT_SKILL.get()


@contextmanager
def bind_skill_context(context: SkillContext) -> Iterator[None]:
    token = _CURRENT_SKILL.set(context)
    try:
        yield
    finally:
        _CURRENT_SKILL.reset(token)
