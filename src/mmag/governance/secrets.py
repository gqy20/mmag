"""Secret access and sensitive-data redaction."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    __str__ = __repr__


class EnvironmentSecretProvider:
    def __init__(self, *, allowed_names: frozenset[str]):
        self.allowed_names = allowed_names

    def get(self, name: str) -> SecretValue:
        if name not in self.allowed_names:
            raise PermissionError(f"secret {name!r} is not allowed")
        value = os.getenv(name)
        if value is None:
            raise KeyError(name)
        return SecretValue(value)


_SENSITIVE = re.compile(r"(?i)(token|api[_-]?key|password|secret)\s*[:=]\s*([^\s,;]+)")


def redact_sensitive(value: str) -> str:
    return _SENSITIVE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
