"""Application service joining LangGraph interrupts to durable approvals."""

from __future__ import annotations

import re
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..capabilities import CapabilityContext, bind_capability_context
from ..governance import GovernanceContext, bind_governance_context
from ..logger import log_context
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


_ARTIFACT_REF = re.compile(r"^artifact://[a-f0-9]{32}$")


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
            supplied_context = {
                "trace_id": capability_context.trace_id,
                "conversation_id": capability_context.conversation_id,
                "message_id": capability_context.message_id,
                "message": capability_context.message,
                "run_id": capability_context.run_id,
                "parent_run_id": capability_context.parent_run_id,
                "workflow_id": capability_context.workflow_id,
                "lifecycle_run_id": capability_context.lifecycle_run_id,
                "installation_id": capability_context.installation_id,
                "tenant_id": capability_context.tenant_id,
                "scope_kind": capability_context.scope_kind,
                "owner_id": capability_context.owner_id,
                "team_id": capability_context.team_id,
                "channel_type": capability_context.channel_type,
            }
            delegated_parent = payload.get("delegated_parent")
            existing_context = payload.get("capability_context")
            if isinstance(delegated_parent, dict):
                payload["capability_context"] = _child_capability_state(
                    payload, delegated_parent
                )
            elif not isinstance(existing_context, dict) or not existing_context:
                payload["capability_context"] = supplied_context
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
                    {"delegated_child": payload["delegated_child"]}
                    if isinstance(payload.get("delegated_child"), dict)
                    else {}
                ),
                **(
                    {"delegated_parent": payload["delegated_parent"]}
                    if isinstance(payload.get("delegated_parent"), dict)
                    else {}
                ),
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
        self._transition_run(
            _approval_run_id(payload), "waiting_approval", str(interruption["id"])
        )
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
        parent_payload, payload = _delegated_payloads(payload)
        thread_id = str(payload["thread_id"])
        if parent_payload is not None:
            child = self.store.runs.get(thread_id)
            expected_parent = _approval_run_id(parent_payload)
            if (
                not expected_parent
                or child.parent_run_id != expected_parent
                or child.actor_id != request.requested_by
                or child.scope_id != request.scope_id
            ):
                raise PermissionError("delegated approval identity does not match its parent run")
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
        lifecycle_run_id = _approval_run_id(payload)
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
            parent_run_id=str(original_context.get("parent_run_id") or ""),
            workflow_id=str(
                original_context.get("workflow_id")
                or original_context.get("run_id")
                or thread_id
            ),
            lifecycle_run_id=str(original_context.get("lifecycle_run_id") or ""),
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
            parent_run_id=context.parent_run_id,
            workflow_id=context.workflow_id,
            lifecycle_run_id=context.lifecycle_run_id,
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
        self._transition_run(lifecycle_run_id, "running", request_id)
        try:
            with (
                log_context.bind(
                    trace_id=context.trace_id,
                    workflow_id=context.workflow_id,
                    run_id=context.run_id,
                    parent_run_id=context.parent_run_id,
                    approval_id=request_id,
                ),
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
            run_id = _stored_run_id(self.store, lifecycle_run_id)
            if run_id is not None:
                self.store.runs.record_failure(
                    run_id, error_code=type(error).__name__
                )
                self._transition_run(lifecycle_run_id, "failed", request_id)
            if parent_payload is not None:
                parent_run_id = _approval_run_id(parent_payload)
                if _stored_run_id(self.store, parent_run_id) is not None:
                    self.store.runs.record_failure(
                        parent_run_id, error_code=type(error).__name__
                    )
                    self._transition_run(parent_run_id, "failed", request_id)
            raise
        if parent_payload is not None:
            return await self._complete_delegated_resume(
                result,
                child_payload=payload,
                parent_payload=parent_payload,
                request_id=request_id,
                request=request,
                context=context,
            )
        run_id = _stored_run_id(self.store, lifecycle_run_id)
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
            self._transition_run(lifecycle_run_id, "succeeded", request_id)
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

    async def _complete_delegated_resume(
        self,
        result: AgentResult,
        *,
        child_payload: dict[str, Any],
        parent_payload: dict[str, Any],
        request_id: str,
        request: ApprovalRequest,
        context: CapabilityContext,
    ) -> AgentResult:
        from .models import AgentRunState

        child_run_id = str(child_payload["thread_id"])
        child = self.store.runs.get(child_run_id)
        envelope = _agent_result_envelope(result, child)
        if result.status is RuntimeStatus.WAITING_APPROVAL:
            self.lifecycle.transition(
                EntityType.AGENT_RUN,
                child_run_id,
                AgentRunState.WAITING_APPROVAL.value,
                command_id=f"delegate:resume-wait:{request_id}:{child_run_id}",
                expected_version=child.version,
                actor_id=request.requested_by,
                trace_id=context.trace_id,
                payload_patch={"dispatch_result": envelope},
            )
            return _attach_delegated_parent(result, parent_payload)

        terminal = (
            AgentRunState.EXHAUSTED
            if result.status is RuntimeStatus.EXHAUSTED
            else AgentRunState.SUCCEEDED
        )
        self.lifecycle.transition(
            EntityType.AGENT_RUN,
            child_run_id,
            terminal.value,
            command_id=f"delegate:resume-finish:{request_id}:{child_run_id}",
            expected_version=child.version,
            actor_id=request.requested_by,
            trace_id=context.trace_id,
            payload_patch={
                "runtime_status": terminal.value,
                "dispatch_result": envelope,
            },
        )
        parent_run_id = child.parent_run_id
        parent = self.store.runs.get(parent_run_id)
        if parent.state is not AgentRunState.WAITING_CHILD:
            raise RuntimeError("delegated parent AgentRun is not waiting for its child")
        queued = self.lifecycle.transition(
            EntityType.AGENT_RUN,
            parent_run_id,
            AgentRunState.QUEUED.value,
            command_id=f"delegate:resume-parent-queue:{request_id}:{parent_run_id}",
            expected_version=parent.version,
            actor_id=request.requested_by,
            trace_id=context.trace_id,
        )
        self.lifecycle.transition(
            EntityType.AGENT_RUN,
            parent_run_id,
            AgentRunState.RUNNING.value,
            command_id=f"delegate:resume-parent-run:{request_id}:{parent_run_id}",
            expected_version=queued.version,
            actor_id=request.requested_by,
            trace_id=context.trace_id,
        )

        parent_context = _resume_context(parent_payload, request, context.trace_id)
        governance = _governance_context(parent_payload, request, parent_context)
        parent_skill = self._restore_skill_context(parent_payload)
        skill_scope = (
            bind_skill_context(parent_skill) if parent_skill is not None else nullcontext()
        )
        self._transition_run(parent_run_id, "running", request_id)
        try:
            with (
                log_context.bind(
                    trace_id=parent_context.trace_id,
                    workflow_id=parent_context.workflow_id,
                    run_id=parent_context.run_id,
                    parent_run_id=parent_context.parent_run_id,
                    approval_id=request_id,
                ),
                bind_capability_context(parent_context),
                skill_scope,
                bind_governance_context(governance),
            ):
                parent_result = await self.gateway.resume(
                    str(parent_payload["thread_id"]),
                    {
                        "delegated_child": {"run_id": child_run_id},
                        "runtime_snapshot": parent_payload.get("runtime_snapshot", {}),
                        "access_context": {
                            "actor_id": request.requested_by,
                            "scope": request.scope_id,
                            "installation_id": parent_context.installation_id,
                            "tenant_id": parent_context.tenant_id,
                        },
                    },
                )
        except Exception as error:
            self.store.runs.record_failure(parent_run_id, error_code=type(error).__name__)
            self._transition_run(parent_run_id, "failed", request_id)
            raise
        if parent_result.status is not RuntimeStatus.WAITING_APPROVAL:
            self._transition_run(parent_run_id, "succeeded", request_id)
        return parent_result

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
        entities: tuple[tuple[EntityType, str], ...]
        if thread_id.startswith("mattermost:"):
            event_id = thread_id.removeprefix("mattermost:")
            entities = (
                (EntityType.AGENT_RUN, f"run:{event_id}"),
                (EntityType.TASK, f"task:{event_id}"),
            )
        else:
            try:
                run = self.store.runs.get(thread_id)
            except KeyError:
                return
            entities = (
                ((EntityType.AGENT_RUN, thread_id),)
                if run.parent_run_id
                else (
                    (EntityType.AGENT_RUN, thread_id),
                    (EntityType.TASK, f"task:{thread_id.removeprefix('run:')}"),
                )
            )
        for entity_type, entity_id in entities:
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
            elif target != "running" and (
                entity.state == "running"
                or (
                    target in {"failed", "cancelled"}
                    and entity.state in {"waiting_child", "waiting_approval"}
                )
            ):
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


def _delegated_payloads(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    delegated = payload.get("delegated_child")
    if isinstance(delegated, dict):
        child = delegated.get("resume")
        if not isinstance(child, dict):
            raise ValueError("delegated approval has no child resume snapshot")
        child = dict(child)
        child["thread_id"] = str(delegated.get("run_id") or child.get("thread_id") or "")
        child["interrupt_id"] = str(
            delegated.get("interrupt_id") or child.get("interrupt_id") or ""
        )
        child["capability_context"] = _child_capability_state(child, payload)
        return dict(payload), child
    parent = payload.get("delegated_parent")
    if isinstance(parent, dict):
        child = dict(payload)
        child.pop("delegated_parent", None)
        return dict(parent), child
    return None, payload


def _child_capability_state(
    child_payload: dict[str, Any], parent_payload: dict[str, Any]
) -> dict[str, Any]:
    snapshot = child_payload.get("runtime_snapshot", {})
    runtime_context = snapshot.get("context", {}) if isinstance(snapshot, dict) else {}
    runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
    parent_context = parent_payload.get("capability_context", {})
    parent_context = parent_context if isinstance(parent_context, dict) else {}
    return {
        **parent_context,
        **runtime_context,
        "run_id": str(child_payload.get("thread_id") or runtime_context.get("run_id") or ""),
        "parent_run_id": str(parent_context.get("lifecycle_run_id") or ""),
        "workflow_id": str(
            parent_context.get("workflow_id") or parent_context.get("run_id") or ""
        ),
        "lifecycle_run_id": str(
            child_payload.get("thread_id") or runtime_context.get("run_id") or ""
        ),
    }


def _resume_context(
    payload: dict[str, Any], request: ApprovalRequest, trace_id: str
) -> CapabilityContext:
    state = payload.get("capability_context", {})
    state = state if isinstance(state, dict) else {}
    thread_id = str(payload.get("thread_id") or "")
    return CapabilityContext(
        trace_id=str(state.get("trace_id") or trace_id),
        actor_id=request.requested_by,
        conversation_id=str(state.get("conversation_id") or scope_resource_id(request.scope_id)),
        message_id=str(state.get("message_id") or request.id),
        message=str(state.get("message") or "approval resume"),
        scope=request.scope_id,
        allowed_capabilities=frozenset(
            str(name)
            for name in _governance_values(payload, "allowed_capabilities")
            if isinstance(name, str) and name
        ),
        run_id=str(state.get("run_id") or thread_id),
        parent_run_id=str(state.get("parent_run_id") or ""),
        workflow_id=str(state.get("workflow_id") or state.get("run_id") or thread_id),
        lifecycle_run_id=str(state.get("lifecycle_run_id") or ""),
        allowed_execution_profiles=frozenset(
            str(ref)
            for ref in payload.get("execution_profiles", ())
            if isinstance(ref, str) and ref
        ),
        installation_id=str(state.get("installation_id") or ""),
        tenant_id=str(state.get("tenant_id") or ""),
        scope_kind=str(state.get("scope_kind") or ""),
        owner_id=str(state.get("owner_id") or ""),
        team_id=str(state.get("team_id") or ""),
        channel_type=str(state.get("channel_type") or ""),
    )


def _governance_values(payload: dict[str, Any], key: str) -> tuple[Any, ...]:
    governance = payload.get("governance_context", {})
    governance = governance if isinstance(governance, dict) else {}
    values = governance.get(key, ())
    return tuple(values) if isinstance(values, (list, tuple)) else ()


def _governance_context(
    payload: dict[str, Any], request: ApprovalRequest, context: CapabilityContext
) -> GovernanceContext:
    governance = payload.get("governance_context", {})
    governance = governance if isinstance(governance, dict) else {}
    return GovernanceContext(
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
            str(role)
            for role in _governance_values(payload, "roles")
            if isinstance(role, str) and role
        ),
        policy_ref=str(governance.get("policy_ref") or ""),
        allowed_capabilities=tuple(
            str(name)
            for name in _governance_values(payload, "allowed_capabilities")
            if isinstance(name, str) and name
        ),
    )


def _attach_delegated_parent(
    result: AgentResult, parent_payload: dict[str, Any]
) -> AgentResult:
    interruptions = []
    for interruption in result.interruptions:
        item = dict(interruption)
        value = item.get("value", {})
        if isinstance(value, dict):
            item["value"] = {**value, "delegated_parent": parent_payload}
        interruptions.append(item)
    return replace(result, interruptions=tuple(interruptions))


def _agent_result_envelope(result: AgentResult, child: Any) -> dict[str, Any]:
    previous = dict(child.result_envelope)
    artifact_refs = tuple(
        str(item.get("ref"))
        for item in result.artifacts
        if isinstance(item, dict)
        and isinstance(item.get("ref"), str)
        and _ARTIFACT_REF.fullmatch(str(item["ref"]))
    )
    return {
        **previous,
        "schema_version": "1.0",
        "status": result.status.value,
        "result": dict(result.output or {}),
        "artifact_refs": list(artifact_refs),
        "provenance": dict(previous.get("provenance") or child.package_snapshot),
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
            "model_calls": result.usage.model_calls,
            "tool_calls": result.usage.tool_calls,
        },
        "interruptions": [dict(item) for item in result.interruptions],
    }


def _lifecycle_run_id(thread_id: str) -> str | None:
    if not thread_id.startswith("mattermost:"):
        return None
    return f"run:{thread_id.removeprefix('mattermost:')}"


def _approval_run_id(payload: dict[str, Any]) -> str:
    state = payload.get("capability_context", {})
    state = state if isinstance(state, dict) else {}
    lifecycle_run_id = str(state.get("lifecycle_run_id") or "")
    if lifecycle_run_id:
        return lifecycle_run_id
    thread_id = str(payload.get("thread_id") or "")
    return _lifecycle_run_id(thread_id) or thread_id


def _stored_run_id(store: SQLiteControlPlane, thread_id: str) -> str | None:
    try:
        store.runs.get(thread_id)
    except KeyError:
        return None
    return thread_id
