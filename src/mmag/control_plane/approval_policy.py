"""Approval actor authorization independent from the chat command parser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .context import MattermostScopeResolver
from .models import ScopeKind

if TYPE_CHECKING:
    from ..client import MMClient
    from .models import ApprovalRequest


class ApprovalAuthorizer(Protocol):
    async def can_decide(self, request: ApprovalRequest, actor_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class StaticApprovalAuthorizer:
    """Explicit actor allowlist, useful for service integrations and tests."""

    actor_ids: frozenset[str]

    async def can_decide(self, request: ApprovalRequest, actor_id: str) -> bool:
        del request
        return actor_id in self.actor_ids


class MattermostApprovalAuthorizer:
    """Allow the requester, a channel admin, or a system administrator."""

    def __init__(self, client: MMClient):
        self.client = client

    async def can_decide(self, request: ApprovalRequest, actor_id: str) -> bool:
        if not actor_id:
            return False
        try:
            _, _, kind, resource_id = MattermostScopeResolver.parse(request.scope_id)
        except ValueError:
            return False
        if kind is ScopeKind.PERSONAL:
            return actor_id == request.requested_by == resource_id
        channel_id = resource_id
        try:
            member = await self.client.get_channel_member_async(channel_id, actor_id)
            user = await self.client.get_user_authorization_async(actor_id)
        except Exception:
            return False
        member_roles = frozenset(str(member.get("roles", "")).split())
        user_roles = frozenset(str(user.get("roles", "")).split())
        return (
            actor_id == request.requested_by
            or "channel_admin" in member_roles
            or "system_admin" in user_roles
        )
