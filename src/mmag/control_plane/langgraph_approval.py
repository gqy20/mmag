"""Application service joining LangGraph interrupts to durable approvals."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityContext, bind_capability_context
from ..governance import GovernanceContext, bind_governance_context
from ..runtimes import RuntimeStatus
from ..skill_packages import SkillContext, bind_skill_context
from .context import scope_resource_id
from .models import EntityType

if TYPE_CHECKING:
    from ..governance import ModelGateway
    from ..runtimes import AgentResult
    from ..skill_packages import SkillPackageRegistry
    from .approval import ApprovalService
    from .approval_policy import ApprovalAuthorizer
    from .context import MattermostAccessGuard
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
        *,
        authorizer: ApprovalAuthorizer,
        access_guard: MattermostAccessGuard,
        skill_registry: SkillPackageRegistry,
    ) -> None:
        self.store = store
        self.lifecycle = lifecycle
        self.approvals = approvals
        self.gateway = gateway
        self.skill_registry = skill_registry
        self.authorizer = authorizer
        self.access_guard = access_guard

    def register(
        self,
        result: AgentResult,
        *,
        requested_by: str,
        scope_id: str,
        capability_context: CapabilityContext | None = None,
    ) -> ApprovalRequest:
        interruption = dict(result.interruptions[0])
        payload = dict(interruption["value"])
        tool_calls = list(payload.get("tool_calls", ()))
        names = ", ".join(str(call.get("capability", "?")) for call in tool_calls)
        if capability_context is not None:
            payload["capability_context"] = {
                "trace_id": capability_context.trace_id,
                "conversation_id": capability_context.conversation_id,
                "message_id": capability_context.message_id,
                "message": capability_context.message,
                "run_id": capability_context.run_id,
                "installation_id": capability_context.installation_id,
                "tenant_id": capability_context.tenant_id,
                "scope_kind": capability_context.scope_kind,
                "owner_id": capability_context.owner_id,
                "team_id": capability_context.team_id,
                "channel_type": capability_context.channel_type,
            }
        approval = self.approvals.request(
            names or "langgraph_tool_batch",
            {
                "thread_id": payload["thread_id"],
                "runtime": payload.get("runtime", "langgraph"),
                "interrupt_id": interruption["id"],
                "tool_calls": tool_calls,
                "runtime_snapshot": payload.get("runtime_snapshot", {}),
                "governance_context": payload.get("governance_context", {}),
                "skill_context": payload.get("skill_context", {}),
                "execution_profiles": payload.get("execution_profiles", []),
                **(
                    {"capability_context": payload["capability_context"]}
                    if "capability_context" in payload
                    else {}
                ),
            },
            requested_by=requested_by,
            scope_id=scope_id,
            resume_token=str(interruption["id"]),
        )
        self._transition_run(str(payload["thread_id"]), "waiting_approval", str(interruption["id"]))
        self.store.append_audit(
            "approval.requested",
            actor_id=requested_by,
            scope_id=scope_id,
            target=approval.id,
            decision="pending",
            details={"capability": approval.capability_name},
        )
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
        payload = dict(request.arguments)
        if request.resume_token != str(payload.get("interrupt_id", "")):
            raise PermissionError("approval resume token is invalid")
        if not await self.authorizer.can_decide(request, actor_id):
            self.store.append_audit(
                "approval.denied",
                actor_id=actor_id,
                scope_id=scope_id,
                trace_id=trace_id,
                target=request_id,
                decision="unauthorized",
            )
            raise PermissionError("actor is not authorized to decide this approval")
        await self.access_guard.require(request.requested_by, request.scope_id)
        request = self.approvals.decide(
            request_id, approved=approved, actor_id=actor_id, reason=reason
        )
        self.store.append_audit(
            "approval.decided",
            actor_id=actor_id,
            scope_id=scope_id,
            trace_id=trace_id,
            target=request_id,
            decision=request.state.value,
            details={"requested_by": request.requested_by},
        )
        decisions = _decisions(payload, approved)
        thread_id = str(payload["thread_id"])
        original_context = payload.get("capability_context", {})
        if not isinstance(original_context, dict):
            original_context = {}
        context = CapabilityContext(
            trace_id=str(original_context.get("trace_id") or trace_id),
            actor_id=request.requested_by,
            conversation_id=str(
                original_context.get("conversation_id") or scope_resource_id(request.scope_id)
            ),
            message_id=str(original_context.get("message_id") or request_id),
            message=str(original_context.get("message") or "approval resume"),
            scope=request.scope_id,
            run_id=str(original_context.get("run_id") or thread_id),
            installation_id=str(original_context.get("installation_id") or ""),
            tenant_id=str(original_context.get("tenant_id") or ""),
            scope_kind=str(original_context.get("scope_kind") or ""),
            owner_id=str(original_context.get("owner_id") or ""),
            team_id=str(original_context.get("team_id") or ""),
            channel_type=str(original_context.get("channel_type") or ""),
        )
        governance = payload.get("governance_context", {})
        if not isinstance(governance, dict):
            governance = {}
        allowed_capabilities = governance.get("allowed_capabilities", ())
        execution_profiles = payload.get("execution_profiles", ())
        roles = governance.get("roles", ())
        if not isinstance(allowed_capabilities, (list, tuple)):
            allowed_capabilities = ()
        if not isinstance(roles, (list, tuple)):
            roles = ()
        if not isinstance(execution_profiles, (list, tuple)):
            execution_profiles = ()
        skill_context = self._restore_skill_context(payload)
        skill_scope = (
            bind_skill_context(skill_context)
            if skill_context is not None
            else nullcontext()
        )
        context = CapabilityContext(
            trace_id=context.trace_id,
            actor_id=context.actor_id,
            conversation_id=context.conversation_id,
            message_id=context.message_id,
            message=context.message,
            scope=context.scope,
            allowed_capabilities=frozenset(
                str(name) for name in allowed_capabilities if isinstance(name, str) and name
            ),
            run_id=context.run_id,
            allowed_execution_profiles=frozenset(
                str(ref) for ref in execution_profiles if isinstance(ref, str) and ref
            ),
            installation_id=context.installation_id,
            tenant_id=context.tenant_id,
            scope_kind=context.scope_kind,
            owner_id=context.owner_id,
            team_id=context.team_id,
            channel_type=context.channel_type,
        )
        self._transition_run(thread_id, "running", request_id)
        try:
            with (
                bind_capability_context(context),
                skill_scope,
                bind_governance_context(
                    GovernanceContext(
                        request.requested_by,
                        request.scope_id,
                        resources={
                            "actor_id": request.requested_by,
                            "conversation_id": context.conversation_id,
                            "installation_id": context.installation_id,
                            "tenant_id": context.tenant_id,
                            "scope_kind": context.scope_kind,
                            "owner_id": context.owner_id,
                            "team_id": context.team_id,
                            "channel_type": context.channel_type,
                        },
                        roles=frozenset(
                            str(role) for role in roles if isinstance(role, str) and role
                        ),
                        policy_ref=str(governance.get("policy_ref") or ""),
                        allowed_capabilities=tuple(
                            str(name)
                            for name in allowed_capabilities
                            if isinstance(name, str) and name
                        ),
                    )
                ),
            ):
                result = await self.gateway.resume(
                    thread_id,
                    {
                        "decisions": decisions,
                        "runtime_snapshot": payload.get("runtime_snapshot", {}),
                        "access_context": {
                            "actor_id": request.requested_by,
                            "scope": request.scope_id,
                            "installation_id": context.installation_id,
                            "tenant_id": context.tenant_id,
                        },
                    },
                )
        except Exception as error:
            run_id = _lifecycle_run_id(thread_id)
            if run_id is not None:
                self.store.runs.record_failure(
                    run_id, error_code=type(error).__name__
                )
            raise
        run_id = _lifecycle_run_id(thread_id)
        if run_id is not None:
            self.store.runs.record_result(
                run_id,
                status=result.status.value,
                usage={
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cost_usd": result.usage.cost_usd,
                    "model_calls": result.usage.model_calls,
                    "tool_calls": result.usage.tool_calls,
                    "repair_calls": result.usage.repair_calls,
                },
                capability_calls=len(result.capability_calls),
                artifact_count=len(result.artifacts),
            )
        if result.status is not RuntimeStatus.WAITING_APPROVAL:
            self._transition_run(thread_id, "succeeded", request_id)
            if skill_context is not None:
                self.store.append_audit(
                    "skill.resumed",
                    actor_id=request.requested_by,
                    scope_id=request.scope_id,
                    trace_id=context.trace_id,
                    target=skill_context.skill_ref,
                    decision="completed",
                    details={
                        **skill_context.package.snapshot.to_dict(),
                        "personal_skill_ref": skill_context.personal_ref,
                        "personal_skill_hash": skill_context.personal_hash,
                    },
                )
        return result

    def _restore_skill_context(
        self,
        payload: dict[str, Any],
    ) -> SkillContext | None:
        state = payload.get("skill_context", {})
        if not isinstance(state, dict) or not state:
            return None
        skill_ref = state.get("skill_ref")
        if not isinstance(skill_ref, str) or not skill_ref:
            raise ValueError("approval contains an invalid Skill context")
        return SkillContext(
            self.skill_registry.get(skill_ref),
            personal_ref=str(state.get("personal_skill_ref") or ""),
            personal_hash=str(state.get("personal_skill_hash") or ""),
        )

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
    if payload.get("runtime") == "deepagents":
        decision = "approve" if approved else "reject"
        return [{"type": decision} for _ in payload.get("tool_calls", ())]
    return [
        {
            "tool_call_id": str(call["tool_call_id"]),
            "decision": "approve" if approved else "reject",
        }
        for call in payload.get("tool_calls", ())
    ]


def _lifecycle_run_id(thread_id: str) -> str | None:
    if not thread_id.startswith("mattermost:"):
        return None
    return f"run:{thread_id.removeprefix('mattermost:')}"
