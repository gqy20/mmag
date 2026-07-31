"""Durable approval pause and resume workflow."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any

from .models import ApprovalRequest, EntityType

if TYPE_CHECKING:
    from .lifecycle import LifecycleService
    from .store import SQLiteControlPlane


class ApprovalService:
    def __init__(self, store: SQLiteControlPlane, lifecycle: LifecycleService):
        self.store = store
        self.lifecycle = lifecycle

    def request(
        self,
        capability_name: str,
        arguments: dict[str, Any],
        *,
        requested_by: str,
        scope_id: str = "",
        ttl_seconds: float | None = 3600,
    ) -> ApprovalRequest:
        now = time.time()
        request = ApprovalRequest(
            id=uuid.uuid4().hex,
            capability_name=capability_name,
            arguments=dict(arguments),
            resume_token=uuid.uuid4().hex,
            requested_by=requested_by,
            scope_id=scope_id,
            expires_at=now + ttl_seconds if ttl_seconds is not None else None,
        )
        self.store.create_approval_request(request)
        return request

    def decide(
        self,
        request_id: str,
        *,
        approved: bool,
        actor_id: str,
        reason: str = "",
    ) -> ApprovalRequest:
        request = self.store.get_approval_request(request_id)
        if request.expires_at is not None and request.expires_at <= time.time():
            target = "expired"
        else:
            target = "approved" if approved else "rejected"
        self.lifecycle.transition(
            EntityType.APPROVAL_REQUEST,
            request_id,
            target,
            command_id=f"approval:{request_id}:{target}",
            actor_id=actor_id,
            reason=reason,
        )
        self.store.record_approval_decision(request_id, actor_id, reason)
        return self.store.get_approval_request(request_id)
