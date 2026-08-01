"""Stable errors exposed by the Agent Package boundary."""


class AgentPackageError(Exception):
    """Base error for package loading and execution."""


class ManifestValidationError(AgentPackageError):
    """The package manifest does not satisfy the v1 contract."""


class PackageReferenceError(AgentPackageError):
    """A package reference is unsafe, missing, or malformed."""


class PromptContractError(AgentPackageError):
    """A prompt declaration or render violates its variable contract."""


class SchemaContractError(AgentPackageError):
    """An input or output violates a package JSON Schema."""

    code = "INVALID_CONTRACT"

    def __init__(self, message: str, *, direction: str):
        super().__init__(message)
        self.direction = direction
