"""Stable errors exposed by the Skill Package boundary."""


class SkillPackageError(Exception):
    """Base error for Skill Package loading and resolution."""


class SkillManifestError(SkillPackageError):
    """The Skill manifest does not satisfy the v1 contract."""


class SkillReferenceError(SkillPackageError):
    """A Skill resource reference is unsafe, missing, or malformed."""


class SkillResolutionError(SkillPackageError):
    """An Agent cannot safely select the requested Skill."""


class SkillContractError(SkillPackageError):
    """A Skill input or output violates its JSON Schema."""

    def __init__(self, message: str, *, direction: str):
        super().__init__(message)
        self.direction = direction
