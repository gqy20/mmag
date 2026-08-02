"""Platform-neutral scope resolution and enterprise context assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from .models import EnterpriseContext, InboundEvent, Principal, Scope, ScopeKind

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


class MattermostScopeResolver:
    """Derive non-forgeable MMAG scopes from authenticated Mattermost posts."""

    _CHANNEL_TYPES = frozenset({"O", "P", "D", "G"})

    def __init__(
        self,
        client,
        *,
        installation_id: str,
        tenant_id: str,
    ) -> None:
        if not installation_id or not tenant_id:
            raise ValueError("Mattermost installation and tenant IDs are required")
        self.client = client
        self.installation_id = installation_id
        self.tenant_id = tenant_id

    def principal(self, actor_id: str) -> Principal:
        return Principal("mattermost", self.installation_id, self.tenant_id, actor_id)

    @staticmethod
    def scope_id(
        installation_id: str,
        tenant_id: str,
        kind: ScopeKind,
        resource_id: str,
    ) -> str:
        resource_kind = "usr" if kind is ScopeKind.PERSONAL else "chn"
        return ":".join(
            (
                "mattermost",
                quote(installation_id, safe=""),
                quote(tenant_id, safe=""),
                resource_kind,
                quote(resource_id, safe=""),
            )
        )

    def resolve_post(self, post: Mapping[str, Any]) -> Scope:
        actor_id = str(post.get("user_id") or "")
        channel_id = str(post.get("channel_id") or "")
        if not actor_id or not channel_id:
            raise ValueError("Mattermost post identity is incomplete")
        channel = self.client.get_channel(channel_id)
        if str(channel.get("id") or "") != channel_id:
            raise PermissionError("Mattermost channel metadata is unavailable")
        channel_type = str(channel.get("type") or "")
        if channel_type not in self._CHANNEL_TYPES:
            raise PermissionError("Mattermost channel type is unavailable")
        team_id = str(channel.get("team_id") or "")
        kind = ScopeKind.PERSONAL if channel_type == "D" else ScopeKind.CHANNEL
        owner_id = actor_id if kind is ScopeKind.PERSONAL else ""
        resource_id = owner_id or channel_id
        scope_id = self.scope_id(
            self.installation_id,
            self.tenant_id,
            kind,
            resource_id,
        )
        return Scope(
            id=scope_id,
            organization_id=self.tenant_id,
            conversation_id=channel_id,
            platform="mattermost",
            installation_id=self.installation_id,
            tenant_id=self.tenant_id,
            kind=kind,
            owner_id=owner_id,
            team_id=team_id,
            channel_type=channel_type,
        )

    @staticmethod
    def parse(scope_id: str) -> tuple[str, str, ScopeKind, str]:
        parts = scope_id.split(":")
        if (
            len(parts) != 5
            or parts[0] != "mattermost"
            or parts[3] not in {"usr", "chn"}
        ):
            raise ValueError("invalid Mattermost scope")
        return (
            unquote(parts[1]),
            unquote(parts[2]),
            ScopeKind.PERSONAL if parts[3] == "usr" else ScopeKind.CHANNEL,
            unquote(parts[4]),
        )


def scope_resource_id(scope_id: str) -> str:
    try:
        return MattermostScopeResolver.parse(scope_id)[3]
    except ValueError:
        return scope_id.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


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
