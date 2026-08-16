"""Runtime-neutral capability contracts and execution policy."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from .sources import enrich_with_sources

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class CapabilityEffect(StrEnum):
    """The externally observable effect of a capability."""

    READ = "read"
    WRITE = "write"


class SourcePolicy(StrEnum):
    """Whether execution results should carry source metadata."""

    NONE = "none"
    AUTO = "auto"


class CapabilityStatus(StrEnum):
    """Stable outcome codes shared by every runtime binding."""

    SUCCESS = "success"
    INVALID_INPUT = "invalid_input"
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"
    TIMEOUT = "timeout"
    ERROR = "error"


class AuthorizationDecision(StrEnum):
    """Deterministic authorization outcomes before side effects begin."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


def _freeze(value: Any) -> Any:
    """Recursively freeze JSON-like metadata owned by a capability spec."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class CapabilitySpec:
    """One capability definition independent of its transport/runtime."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[..., Any] = field(repr=False, compare=False)
    effect: CapabilityEffect = CapabilityEffect.READ
    permission: str = ""
    timeout_seconds: float = 10
    source_policy: SourcePolicy = SourcePolicy.NONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _freeze(dict(self.input_schema)))


@dataclass(frozen=True)
class CapabilityResult:
    """Transport-neutral result returned by the capability executor."""

    status: CapabilityStatus
    data: Any = None
    message: str = ""
    duration_ms: int = 0

    def to_payload(self) -> Any:
        """Render the stable public payload consumed by runtime bindings."""
        if self.status is CapabilityStatus.SUCCESS:
            return self.data
        return {
            "error": {
                "code": self.status.value,
                "message": self.message,
            }
        }


@dataclass(frozen=True)
class CapabilitySuspension:
    """Runtime-neutral request to pause the current capability invocation."""

    value: Mapping[str, Any]


@dataclass(frozen=True)
class CapabilityAuthorization:
    """Policy decision returned before a capability handler runs."""

    decision: AuthorizationDecision
    reason: str = ""

    @classmethod
    def allow(cls) -> CapabilityAuthorization:
        return cls(AuthorizationDecision.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> CapabilityAuthorization:
        return cls(AuthorizationDecision.DENY, reason)

    @classmethod
    def require_approval(cls, reason: str) -> CapabilityAuthorization:
        return cls(AuthorizationDecision.REQUIRE_APPROVAL, reason)


class CapabilityAuthorizer(Protocol):
    """Policy port evaluated before any capability side effect."""

    def authorize(
        self,
        spec: CapabilitySpec,
        arguments: Mapping[str, Any],
    ) -> CapabilityAuthorization: ...


class CapabilityExecutor:
    """Validate and execute a capability with one timeout/error policy."""

    def __init__(self, authorizer: CapabilityAuthorizer) -> None:
        self.authorizer = authorizer

    def authorize(
        self, spec: CapabilitySpec, arguments: Mapping[str, Any]
    ) -> CapabilityAuthorization:
        """Validate and authorize without invoking the capability handler."""
        validation_error = _validate_arguments(spec.input_schema, arguments)
        if validation_error:
            return CapabilityAuthorization.deny(validation_error)
        return self.authorizer.authorize(spec, arguments)

    async def execute(self, spec: CapabilitySpec, arguments: Mapping[str, Any]) -> CapabilityResult:
        started_at = time.monotonic()
        invalid = self._invalid_result(spec, arguments, started_at)
        if invalid is not None:
            return invalid
        authorization = self.authorizer.authorize(spec, arguments)
        blocked = self._blocked_result(spec, authorization, started_at)
        if blocked is not None:
            return blocked
        return await self._invoke(spec, arguments, started_at)

    async def execute_approved(
        self, spec: CapabilitySpec, arguments: Mapping[str, Any]
    ) -> CapabilityResult:
        """Execute a previously approved call without evaluating policy twice."""
        started_at = time.monotonic()
        invalid = self._invalid_result(spec, arguments, started_at)
        if invalid is not None:
            return invalid
        return await self._invoke(spec, arguments, started_at)

    async def _invoke(
        self,
        spec: CapabilitySpec,
        arguments: Mapping[str, Any],
        started_at: float,
    ) -> CapabilityResult:
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                value = spec.handler(**dict(arguments))
                if inspect.isawaitable(value):
                    value = await value
                value = _apply_source_policy(spec, value, dict(arguments))
        except TimeoutError:
            return self._result(
                started_at,
                CapabilityStatus.TIMEOUT,
                message=f"Capability '{spec.name}' timed out",
            )
        except Exception as exc:
            return self._result(
                started_at,
                CapabilityStatus.ERROR,
                message=f"Capability '{spec.name}' failed: {exc}",
            )
        return self._result(started_at, CapabilityStatus.SUCCESS, data=value)

    def _invalid_result(
        self,
        spec: CapabilitySpec,
        arguments: Mapping[str, Any],
        started_at: float,
    ) -> CapabilityResult | None:
        error = _validate_arguments(spec.input_schema, arguments)
        if error is None:
            return None
        return self._result(started_at, CapabilityStatus.INVALID_INPUT, message=error)

    def _blocked_result(
        self,
        spec: CapabilitySpec,
        authorization: CapabilityAuthorization,
        started_at: float,
    ) -> CapabilityResult | None:
        if authorization.decision is AuthorizationDecision.DENY:
            message = authorization.reason or f"Capability '{spec.name}' is forbidden"
            return self._result(started_at, CapabilityStatus.FORBIDDEN, message=message)
        if authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL:
            message = authorization.reason or f"Capability '{spec.name}' requires approval"
            return self._result(started_at, CapabilityStatus.APPROVAL_REQUIRED, message=message)
        return None

    @staticmethod
    def _result(
        started_at: float,
        status: CapabilityStatus,
        *,
        data: Any = None,
        message: str = "",
    ) -> CapabilityResult:
        duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
        return CapabilityResult(
            status=status,
            data=data,
            message=message,
            duration_ms=duration_ms,
        )


_JSON_TYPES: dict[str, type[Any] | tuple[type[Any], ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": (list, tuple),
    "object": dict,
}


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    required = schema.get("required", ())
    missing = [name for name in required if name not in arguments]
    if missing:
        return f"Missing required input: {', '.join(missing)}"

    properties = schema.get("properties", {})
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if not property_schema or value is None:
            continue
        expected_name = property_schema.get("type")
        expected_type = _JSON_TYPES.get(expected_name)
        if expected_type is not None and not isinstance(value, expected_type):
            return f"Invalid input '{name}': expected {expected_name}"
    return None


def _apply_source_policy(
    spec: CapabilitySpec,
    value: Any,
    arguments: dict[str, Any],
) -> Any:
    """Attach normalized source metadata when a capability declares AUTO."""
    if spec.source_policy is not SourcePolicy.AUTO:
        return value
    return enrich_with_sources(value, spec.name, arguments)
