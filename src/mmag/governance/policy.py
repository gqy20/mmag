"""Deterministic and explainable capability policy evaluation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from types import MappingProxyType
from typing import TYPE_CHECKING

from ..capabilities import CapabilityAuthorization

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any

    from ..capabilities import CapabilitySpec


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class GovernanceContext:
    actor_id: str
    scope: str
    roles: frozenset[str] = frozenset()
    resources: Mapping[str, str] = field(default_factory=dict)
    policy_ref: str = ""
    allowed_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))


@dataclass(frozen=True, slots=True)
class PolicyRule:
    id: str
    effect: PolicyEffect
    actors: tuple[str, ...] = ("*",)
    scopes: tuple[str, ...] = ("*",)
    permissions: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    actions: tuple[str, ...] = ("*",)
    resource_arguments: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_arguments",
            MappingProxyType(dict(self.resource_arguments)),
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    rule_id: str
    reason: str


class PolicyEngine:
    def __init__(
        self,
        rules: tuple[PolicyRule, ...] = (),
        *,
        default_effect: PolicyEffect = PolicyEffect.DENY,
    ):
        self.rules = rules
        self.default_effect = default_effect

    def evaluate(
        self,
        spec: CapabilitySpec,
        arguments: Mapping[str, Any],
        context: GovernanceContext,
    ) -> PolicyDecision:
        for rule in self.rules:
            if self._matches(rule, spec, arguments, context):
                reason = rule.reason or f"matched policy {rule.id}"
                return PolicyDecision(rule.effect, rule.id, reason)
        return PolicyDecision(self.default_effect, "default", "no explicit policy rule matched")

    @staticmethod
    def _matches(
        rule: PolicyRule,
        spec: CapabilitySpec,
        arguments: Mapping[str, Any],
        context: GovernanceContext,
    ) -> bool:
        return (
            any(fnmatch(spec.name, pattern) for pattern in rule.actions)
            and any(fnmatch(context.actor_id, pattern) for pattern in rule.actors)
            and any(fnmatch(context.scope, pattern) for pattern in rule.scopes)
            and (
                not rule.permissions
                or any(fnmatch(spec.permission, pattern) for pattern in rule.permissions)
            )
            and (not rule.roles or bool(context.roles.intersection(rule.roles)))
            and PolicyEngine._matches_resources(rule, arguments, context)
        )

    @staticmethod
    def _matches_resources(
        rule: PolicyRule,
        arguments: Mapping[str, Any],
        context: GovernanceContext,
    ) -> bool:
        return all(
            argument in arguments
            and resource_name in context.resources
            and arguments[argument] == context.resources[resource_name]
            for argument, resource_name in rule.resource_arguments.items()
        )


_GOVERNANCE_CONTEXT: ContextVar[GovernanceContext | None] = ContextVar(
    "mmag_governance_context", default=None
)


@contextmanager
def bind_governance_context(context: GovernanceContext) -> Iterator[None]:
    token = _GOVERNANCE_CONTEXT.set(context)
    try:
        yield
    finally:
        _GOVERNANCE_CONTEXT.reset(token)


def get_governance_context() -> GovernanceContext | None:
    return _GOVERNANCE_CONTEXT.get()


class PolicyCapabilityAuthorizer:
    """CapabilityAuthorizer adapter used by both runtime bindings."""

    def __init__(self, engine: PolicyEngine):
        self.engine = engine

    def authorize(
        self, spec: CapabilitySpec, arguments: Mapping[str, Any]
    ) -> CapabilityAuthorization:
        context = _GOVERNANCE_CONTEXT.get() or GovernanceContext("anonymous", "*")
        decision = self.engine.evaluate(spec, arguments, context)
        if decision.effect is PolicyEffect.DENY:
            return CapabilityAuthorization.deny(decision.reason)
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            return CapabilityAuthorization.require_approval(decision.reason)
        return CapabilityAuthorization.allow()


class RegistryPolicyAuthorizer:
    """Resolve the active Agent Package policy for every capability call."""

    def __init__(self, registry) -> None:
        self.registry = registry

    def authorize(
        self, spec: CapabilitySpec, arguments: Mapping[str, Any]
    ) -> CapabilityAuthorization:
        context = get_governance_context()
        if context is None or not context.policy_ref:
            return CapabilityAuthorization.deny("capability call has no Agent Package policy")
        if not any(fnmatch(spec.name, name) for name in context.allowed_capabilities):
            return CapabilityAuthorization.deny(
                f"capability {spec.name!r} is outside the Agent Package allowlist"
            )
        decision = self.registry.get(context.policy_ref).evaluate(spec, arguments, context)
        if decision.effect is PolicyEffect.DENY:
            return CapabilityAuthorization.deny(decision.reason)
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            return CapabilityAuthorization.require_approval(decision.reason)
        return CapabilityAuthorization.allow()
