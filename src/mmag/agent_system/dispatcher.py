"""Governed coordination of one Agent Package from another Agent run."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from ..logger import log_context
from .core import AgentOutput, AgentRegistry, AgentRequest

if TYPE_CHECKING:
    from ..agent_packages import AgentPackageRegistry
    from ..capabilities import CapabilityContext
    from ..control_plane.runs import AgentRunService
    from ..skill_packages import SkillResolver


_ARTIFACT_REF = re.compile(r"^artifact://[a-f0-9]{32}$")


class AuditSink(Protocol):
    def append_audit(
        self,
        event_type: str,
        *,
        actor_id: str = "",
        scope_id: str = "",
        trace_id: str = "",
        target: str = "",
        decision: str = "",
        details: dict[str, Any] | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class AgentDispatchTarget:
    agent_name: str
    intent: str
    skill_name: str = ""


@dataclass(frozen=True, slots=True)
class AgentDispatchResult:
    parent_run_id: str
    run_id: str
    agent_name: str
    skill_ref: str
    status: str
    result: dict[str, Any]
    artifact_refs: tuple[str, ...]
    provenance: dict[str, Any]
    interruptions: tuple[dict[str, Any], ...] = ()

    def to_capability_result(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "child_run": {
                "run_id": self.run_id,
                "parent_run_id": self.parent_run_id,
                "agent": self.agent_name,
                "skill_ref": self.skill_ref,
            },
            "result": self.result,
            "artifact_refs": list(self.artifact_refs),
            "provenance": self.provenance,
        }


class RunCoordinator:
    """Own the governed lifecycle and recovery boundary for one child AgentRun."""

    def __init__(
        self,
        agents: AgentRegistry,
        packages: AgentPackageRegistry,
        skills: SkillResolver,
        runs: AgentRunService,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.agents = agents
        self.packages = packages
        self.skills = skills
        self.runs = runs
        self.audit_sink = audit_sink

    async def dispatch(
        self,
        target: AgentDispatchTarget,
        *,
        task: str,
        context: CapabilityContext,
        task_id: str,
    ) -> AgentDispatchResult:
        from ..control_plane.models import AgentRunSpec, AgentRunState

        agent = self.agents.get(target.agent_name)
        package = self.packages.get(target.agent_name)
        parent_thread_id = context.run_id or context.trace_id
        parent_run_id = (
            context.lifecycle_run_id
            if context.lifecycle_run_id.startswith("run:")
            else self._lifecycle_run_id(parent_thread_id)
        )
        workflow_id = context.workflow_id or parent_thread_id
        candidate_run_id = f"delegate:{target.agent_name}:{uuid.uuid4().hex[:16]}"
        request = AgentRequest(
            intent=target.intent,
            prompt=task,
            scope=context.scope,
            actor_id=context.actor_id,
            task_id=task_id,
            run_id=candidate_run_id,
            requested_agent=target.agent_name,
            requested_skill=target.skill_name,
        )
        if target.skill_name:
            skill = self.skills.resolve(package, request, agent.descriptor.capabilities)
            if skill is None:
                raise LookupError(
                    f"Agent {target.agent_name!r} did not resolve Skill {target.skill_name!r}"
                )
            request = replace(request, skill=skill)

        skill_ref = request.skill.ref if request.skill is not None else ""
        snapshot: dict[str, Any] = package.snapshot.to_dict()
        if request.skill is not None:
            snapshot["skill"] = dict(request.skill.provenance)
        spec = AgentRunSpec(
            run_id=candidate_run_id,
            workflow_id=workflow_id,
            parent_run_id=parent_run_id,
            parent_tool_call_id=context.tool_call_id,
            actor_id=context.actor_id,
            scope_id=context.scope,
            trace_id=context.trace_id,
            thread_id=candidate_run_id,
            agent_ref=f"{package.snapshot.agent_name}@{package.snapshot.agent_spec_version}",
            skill_ref=skill_ref,
            package_snapshot=snapshot,
        )
        record, created = self.runs.create_or_get(spec)
        run_id = record.run_id
        request = replace(request, run_id=run_id)
        if not created and record.state is not AgentRunState.QUEUED:
            replay = self._replay(record, target)
            self._audit(
                context,
                target,
                run_id,
                parent_run_id,
                decision="replayed",
                skill_ref=skill_ref,
                artifact_count=len(replay.artifact_refs),
            )
            return replay

        record = self.runs.transition(
            run_id,
            AgentRunState.RUNNING,
            command_id=f"delegate:claim:{run_id}:{uuid.uuid4().hex}",
            expected_version=record.version,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
        )
        parent = self.runs.get(parent_run_id)
        try:
            if parent.state is not AgentRunState.RUNNING:
                raise RuntimeError(
                    f"parent AgentRun {parent_run_id!r} must be running before delegation"
                )
            parent = self.runs.transition(
                parent_run_id,
                AgentRunState.WAITING_CHILD,
                command_id=f"delegate:wait-child:{run_id}",
                expected_version=parent.version,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
            )
        except Exception as error:
            self.runs.fail(
                run_id,
                error_code=type(error).__name__,
                command_id=f"delegate:parent-unavailable:{run_id}",
                expected_version=record.version,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
            )
            raise

        self._audit(
            context,
            target,
            run_id,
            parent_run_id,
            decision="running",
            skill_ref=skill_ref,
        )
        try:
            with log_context.bind(
                workflow_id=workflow_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                execution_key=context.execution_key,
            ):
                output = await agent.run(request)
        except Exception as error:
            self.runs.fail(
                run_id,
                error_code=type(error).__name__,
                command_id=f"delegate:fail:{run_id}",
                expected_version=record.version,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
            )
            self._audit(
                context,
                target,
                run_id,
                parent_run_id,
                decision="failed",
                skill_ref=skill_ref,
                error_code=type(error).__name__,
            )
            self._resume_parent(parent, run_id, context)
            raise

        status = self._runtime_status(output)
        artifact_refs = self._artifact_refs(output)
        provenance = self._provenance(output)
        result_envelope = {
            "schema_version": "1.0",
            "status": status,
            "result": dict(output.result or {}),
            "artifact_refs": list(artifact_refs),
            "provenance": provenance,
            "usage": self._usage(output),
            "interruptions": [dict(item) for item in self._interruptions(output)],
        }
        if status == AgentRunState.WAITING_APPROVAL.value:
            record = self.runs.transition(
                run_id,
                AgentRunState.WAITING_APPROVAL,
                command_id=f"delegate:wait-approval:{run_id}",
                expected_version=record.version,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
                payload_patch={"dispatch_result": result_envelope},
            )
        else:
            terminal = (
                AgentRunState.EXHAUSTED
                if status == AgentRunState.EXHAUSTED.value
                else AgentRunState.SUCCEEDED
            )
            record = self.runs.finish(
                run_id,
                terminal,
                result_envelope=result_envelope,
                command_id=f"delegate:finish:{run_id}",
                expected_version=record.version,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
            )
            self._resume_parent(parent, run_id, context)
        self._audit(
            context,
            target,
            run_id,
            parent_run_id,
            decision=status,
            skill_ref=skill_ref,
            artifact_count=len(artifact_refs),
        )
        return AgentDispatchResult(
            parent_run_id=parent_run_id,
            run_id=run_id,
            agent_name=target.agent_name,
            skill_ref=skill_ref,
            status=status,
            result=dict(output.result or {}),
            artifact_refs=artifact_refs,
            provenance=provenance,
            interruptions=self._interruptions(output),
        )

    def _resume_parent(self, parent, child_run_id: str, context: CapabilityContext) -> None:
        from ..control_plane.models import AgentRunState

        queued = self.runs.transition(
            parent.run_id,
            AgentRunState.QUEUED,
            command_id=f"delegate:child-done:queue:{child_run_id}",
            expected_version=parent.version,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
        )
        self.runs.transition(
            parent.run_id,
            AgentRunState.RUNNING,
            command_id=f"delegate:child-done:run:{child_run_id}",
            expected_version=queued.version,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
        )

    def _audit(
        self,
        context: CapabilityContext,
        target: AgentDispatchTarget,
        run_id: str,
        parent_run_id: str,
        *,
        decision: str,
        skill_ref: str,
        artifact_count: int = 0,
        error_code: str = "",
    ) -> None:
        if self.audit_sink is None:
            return
        self.audit_sink.append_audit(
            "agent.delegated",
            actor_id=context.actor_id,
            scope_id=context.scope,
            trace_id=context.trace_id,
            target=target.agent_name,
            decision=decision,
            details={
                "schema_version": "1.0",
                "parent_run_id": parent_run_id,
                "workflow_id": context.workflow_id or parent_run_id,
                "run_id": run_id,
                "agent": target.agent_name,
                "skill_ref": skill_ref,
                "artifact_count": artifact_count,
                "error_code": error_code,
                "execution_key_sha256": context.execution_key[:16]
                if context.execution_key
                else "",
            },
        )

    @staticmethod
    def _runtime_status(output: AgentOutput) -> str:
        status = getattr(output.runtime_result, "status", None)
        value = getattr(status, "value", status)
        value = str(value or "succeeded")
        return "succeeded" if value == "completed" else value

    @staticmethod
    def _usage(output: AgentOutput) -> dict[str, Any]:
        usage = getattr(output.runtime_result, "usage", None)
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cost_usd": float(getattr(usage, "cost_usd", 0.0) or 0.0),
            "model_calls": int(getattr(usage, "model_calls", 0) or 0),
            "tool_calls": int(getattr(usage, "tool_calls", 0) or 0),
        }

    @staticmethod
    def _replay(record, target: AgentDispatchTarget) -> AgentDispatchResult:
        from ..control_plane.models import AgentRunState
        from ..control_plane.runs import AgentRunInProgressError, AgentRunTerminalError

        if record.state is AgentRunState.RUNNING:
            raise AgentRunInProgressError(f"child AgentRun {record.run_id!r} is running")
        if record.state in {AgentRunState.FAILED, AgentRunState.CANCELLED}:
            raise AgentRunTerminalError(
                f"child AgentRun {record.run_id!r} ended as {record.state.value}"
            )
        envelope = dict(record.result_envelope)
        return AgentDispatchResult(
            parent_run_id=record.parent_run_id,
            run_id=record.run_id,
            agent_name=target.agent_name,
            skill_ref=record.skill_ref,
            status=record.state.value,
            result=dict(envelope.get("result") or {}),
            artifact_refs=tuple(str(ref) for ref in envelope.get("artifact_refs") or ()),
            provenance=dict(envelope.get("provenance") or {}),
            interruptions=tuple(
                dict(item)
                for item in envelope.get("interruptions") or ()
                if isinstance(item, dict)
            ),
        )

    @staticmethod
    def _interruptions(output: AgentOutput) -> tuple[dict[str, Any], ...]:
        interruptions = getattr(output.runtime_result, "interruptions", ())
        return tuple(dict(item) for item in interruptions if isinstance(item, dict))

    @staticmethod
    def _lifecycle_run_id(run_id: str) -> str:
        return (
            f"run:{run_id.removeprefix('mattermost:')}"
            if run_id.startswith("mattermost:")
            else run_id
        )

    @staticmethod
    def _artifact_refs(output: AgentOutput) -> tuple[str, ...]:
        refs = []
        for artifact in output.artifacts:
            ref = artifact.get("ref")
            if isinstance(ref, str) and _ARTIFACT_REF.fullmatch(ref):
                refs.append(ref)
        return tuple(refs)

    @staticmethod
    def _provenance(output: AgentOutput) -> dict[str, Any]:
        envelope = output.envelope or {}
        provenance = envelope.get("provenance")
        return dict(provenance) if isinstance(provenance, dict) else {}
