"""Application service joining LangGraph interrupts to durable approvals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityContext, bind_capability_context
from ..governance import GovernanceContext, bind_governance_context
from ..runtimes import RuntimeStatus
from .models import EntityType

if TYPE_CHECKING:
    from ..governance import ModelGateway
    from ..runtimes import AgentResult
    from .approval import ApprovalService
    from .lifecycle import LifecycleService
    from .models import ApprovalRequest
    from .store import SQLiteControlPlane


class LangGraphApprovalCoordinator:
    """Synchronize graph pause/resume with business lifecycle state."""

    def __init__(
        self,
        store: SQLiteControlPlane,
        lifecycle: LifecycleService,
        approvals: ApprovalService,
        gateway: ModelGateway,
    ) -> None:
        self.store = store
        self.lifecycle = lifecycle
        self.approvals = approvals
        self.gateway = gateway

    def register(
        self,
        result: AgentResult,
        *,
        requested_by: str,
        scope_id: str,
    ) -> ApprovalRequest:
        interruption = dict(result.interruptions[0])
        payload = dict(interruption["value"])
        tool_calls = list(payload.get("tool_calls", ()))
        names = ", ".join(str(call.get("capability", "?")) for call in tool_calls)
        approval = self.approvals.request(
            names or "langgraph_tool_batch",
            {
                "thread_id": payload["thread_id"],
                "interrupt_id": interruption["id"],
                "tool_calls": tool_calls,
            },
            requested_by=requested_by,
            scope_id=scope_id,
            resume_token=str(interruption["id"]),
        )
        self._transition_run(str(payload["thread_id"]), "waiting_approval", str(interruption["id"]))
        return approval

    async def resume(
        self,
        request_id: str,
        *,
        approved: bool,
        actor_id: str,
        scope_id: str,
        trace_id: str,
        reason: str = "",
    ) -> AgentResult:
        request = self.store.get_approval_request(request_id)
        if request.scope_id != scope_id:
            raise PermissionError("approval belongs to another Mattermost scope")
        request = self.approvals.decide(
            request_id,
            approved=approved,
            actor_id=actor_id,
            reason=reason,
        )
        payload = dict(request.arguments)
        decisions = _decisions(payload, approved)
        thread_id = str(payload["thread_id"])
        context = CapabilityContext(
            trace_id=trace_id,
            actor_id=request.requested_by,
            conversation_id=request.scope_id.rsplit("/", 1)[-1],
            message_id=request_id,
            message="approval resume",
            scope=request.scope_id,
        )
        self._transition_run(thread_id, "running", request_id)
        with (
            bind_capability_context(context),
            bind_governance_context(GovernanceContext(request.requested_by, request.scope_id)),
        ):
            result = await self.gateway.resume(thread_id, {"decisions": decisions})
        if result.status is not RuntimeStatus.WAITING_APPROVAL:
            self._transition_run(thread_id, "succeeded", request_id)
        return result

    def _transition_run(self, thread_id: str, target: str, command: str) -> None:
        if not thread_id.startswith("mattermost:"):
            return
        event_id = thread_id.removeprefix("mattermost:")
        for entity_type, prefix in (
            (EntityType.AGENT_RUN, "run"),
            (EntityType.TASK, "task"),
        ):
            entity_id = f"{prefix}:{event_id}"
            try:
                entity = self.store.get_lifecycle_entity(entity_type, entity_id)
            except KeyError:
                continue
            if target == "running" and entity.state in {"waiting_approval", "queued"}:
                if entity_type is EntityType.AGENT_RUN and entity.state == "waiting_approval":
                    entity = self.lifecycle.transition(
                        entity_type,
                        entity_id,
                        "queued",
                        command_id=f"resume-queue:{command}:{entity_id}",
                    )
                if entity.state in {"waiting_approval", "queued"}:
                    self.lifecycle.transition(
                        entity_type,
                        entity_id,
                        "running",
                        command_id=f"resume-run:{command}:{entity_id}",
                        expected_version=entity.version,
                    )
            elif target != "running" and entity.state == "running":
                self.lifecycle.transition(
                    entity_type,
                    entity_id,
                    target,
                    command_id=f"graph-{target}:{command}:{entity_id}",
                    expected_version=entity.version,
                )


def _decisions(payload: dict[str, Any], approved: bool) -> list[dict[str, str]]:
    return [
        {
            "tool_call_id": str(call["tool_call_id"]),
            "decision": "approve" if approved else "reject",
        }
        for call in payload.get("tool_calls", ())
    ]
