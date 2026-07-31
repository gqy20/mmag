"""Platform-neutral scope resolution and enterprise context assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import EnterpriseContext, InboundEvent, Scope

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .store import SQLiteControlPlane


class ScopeResolver:
    def resolve(self, event: InboundEvent) -> Scope:
        payload = event.payload
        organization = str(payload.get("organization_id") or payload.get("team_id") or "")
        project = str(payload.get("project_id") or "")
        customer = str(payload.get("customer_id") or "")
        scope_id = ":".join(
            part for part in (organization, project, customer, event.conversation_id) if part
        )
        return Scope(scope_id, organization, project, customer, event.conversation_id)


@dataclass(frozen=True, slots=True)
class AssembledContext:
    scope: Scope
    entities: tuple[EnterpriseContext, ...]
    attributes: Mapping[str, Any]


class ContextAssembler:
    def __init__(self, store: SQLiteControlPlane):
        self.store = store

    def assemble(
        self, scope: Scope, *, attributes: Mapping[str, Any] | None = None
    ) -> AssembledContext:
        return AssembledContext(
            scope=scope,
            entities=tuple(self.store.get_context(scope.id)),
            attributes=dict(attributes or {}),
        )
