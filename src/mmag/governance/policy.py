"""Deterministic and explainable capability policy evaluation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
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


@dataclass(frozen=True, slots=True)
class PolicyRule:
    id: str
    effect: PolicyEffect
    actors: tuple[str, ...] = ("*",)
    scopes: tuple[str, ...] = ("*",)
    permissions: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    reason: str = ""


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
        default_effect: PolicyEffect = PolicyEffect.ALLOW,
    ):
        self.rules = rules
        self.default_effect = default_effect

    def evaluate(
        self,
        spec: CapabilitySpec,
        arguments: Mapping[str, Any],
        context: GovernanceContext,
    ) -> PolicyDecision:
        del arguments
        for rule in self.rules:
            if self._matches(rule, spec, context):
                reason = rule.reason or f"matched policy {rule.id}"
                return PolicyDecision(rule.effect, rule.id, reason)
        return PolicyDecision(self.default_effect, "default", "no explicit policy rule matched")

    @staticmethod
    def _matches(rule: PolicyRule, spec: CapabilitySpec, context: GovernanceContext) -> bool:
        return (
            any(fnmatch(context.actor_id, pattern) for pattern in rule.actors)
            and any(fnmatch(context.scope, pattern) for pattern in rule.scopes)
            and (not rule.permissions or spec.permission in rule.permissions)
            and (not rule.roles or bool(context.roles.intersection(rule.roles)))
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
