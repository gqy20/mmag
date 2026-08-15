"""Governed dispatch of one Agent Package from another Agent run."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from .core import AgentOutput, AgentRegistry, AgentRequest

if TYPE_CHECKING:
    from ..agent_packages import AgentPackageRegistry
    from ..capabilities import CapabilityContext
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


class AgentDispatcher:
    """Resolve the target's governed Skill and execute one traceable child run."""

    def __init__(
        self,
        agents: AgentRegistry,
        packages: AgentPackageRegistry,
        skills: SkillResolver,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.agents = agents
        self.packages = packages
        self.skills = skills
        self.audit_sink = audit_sink

    async def dispatch(
        self,
        target: AgentDispatchTarget,
        *,
        task: str,
        context: CapabilityContext,
        task_id: str,
    ) -> AgentDispatchResult:
        agent = self.agents.get(target.agent_name)
        package = self.packages.get(target.agent_name)
        parent_run_id = context.run_id or context.trace_id
        run_id = f"delegate:{target.agent_name}:{uuid.uuid4().hex[:16]}"
        request = AgentRequest(
            intent=target.intent,
            prompt=task,
            scope=context.scope,
            actor_id=context.actor_id,
            task_id=task_id,
            run_id=run_id,
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

        self._audit(
            context,
            target,
            run_id,
            parent_run_id,
            decision="running",
            skill_ref=request.skill.ref if request.skill is not None else "",
        )
        try:
            output = await agent.run(request)
        except Exception as error:
            self._audit(
                context,
                target,
                run_id,
                parent_run_id,
                decision="failed",
                skill_ref=request.skill.ref if request.skill is not None else "",
                error_code=type(error).__name__,
            )
            raise

        status = self._runtime_status(output)
        artifact_refs = self._artifact_refs(output)
        provenance = self._provenance(output)
        self._audit(
            context,
            target,
            run_id,
            parent_run_id,
            decision=status,
            skill_ref=request.skill.ref if request.skill is not None else "",
            artifact_count=len(artifact_refs),
        )
        return AgentDispatchResult(
            parent_run_id=parent_run_id,
            run_id=run_id,
            agent_name=target.agent_name,
            skill_ref=request.skill.ref if request.skill is not None else "",
            status=status,
            result=dict(output.result or {}),
            artifact_refs=artifact_refs,
            provenance=provenance,
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
                "run_id": run_id,
                "agent": target.agent_name,
                "skill_ref": skill_ref,
                "artifact_count": artifact_count,
                "error_code": error_code,
            },
        )

    @staticmethod
    def _runtime_status(output: AgentOutput) -> str:
        status = getattr(output.runtime_result, "status", None)
        value = getattr(status, "value", status)
        return str(value or "succeeded")

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
