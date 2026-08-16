"""Delegate capabilities that let mmchat dispatch to sub-agents."""

from __future__ import annotations

from ..agent_system import AgentDispatchTarget, RunCoordinator
from ..logger import get_logger, log_context, log_event, safe_hash
from .base import CapabilityEffect, CapabilitySpec, CapabilitySuspension, SourcePolicy
from .context import get_capability_context

log = get_logger(__name__)

_SUBAGENTS: dict[str, tuple[str, str, str, str]] = {
    # name → (agent_name, description, intent, skill_name)
    "delegate_ppt": (
        "ppt",
        "委托 PPT 子智能体生成演示文稿（pptx 文件）。",
        "presentation",
        "slides",
    ),
    "delegate_report": (
        "report",
        "委托 Report 子智能体生成研究报告或会议纪要。",
        "report",
        "report",
    ),
    "delegate_project": (
        "project",
        "委托 Project 子智能体生成项目计划、里程碑规划或进度简报。",
        "project",
        "project",
    ),
    "delegate_link": (
        "link",
        "委托 Link 子智能体分析指定 URL 的网页内容。",
        "link",
        "",
    ),
}


def _create_delegate_capability(
    cap_name: str,
    agent_name: str,
    description: str,
    intent: str,
    skill_name: str,
    coordinator: RunCoordinator,
) -> CapabilitySpec:
    async def delegate(task: str) -> dict | CapabilitySuspension:
        parent_context = get_capability_context()
        if parent_context is None or not parent_context.actor_id or not parent_context.scope:
            return {"status": "error", "error": "缺少可信的子智能体委托上下文"}

        log_event(
            log,
            "delegate.started",
            status="running",
            subagent=agent_name,
            input_sha256=safe_hash(task),
        )

        try:
            result = await coordinator.dispatch(
                AgentDispatchTarget(agent_name, intent, skill_name),
                task=task,
                context=parent_context,
                task_id=log_context.get("task_id", parent_context.trace_id),
            )
        except Exception as error:
            log_event(
                log,
                "delegate.failed",
                level=40,
                status="failed",
                subagent=agent_name,
                error_code=type(error).__name__,
            )
            return {"status": "error", "error": f"子智能体执行失败: {type(error).__name__}"}

        log_event(
            log,
            "delegate.completed",
            status=result.status,
            subagent=agent_name,
            child_run_id=result.run_id,
            artifact_count=len(result.artifact_refs),
        )
        if result.status == "waiting_approval":
            if not result.interruptions:
                raise RuntimeError("child Agent paused without an interrupt payload")
            child_interrupt = dict(result.interruptions[0])
            child_resume = child_interrupt.get("value", {})
            if not isinstance(child_resume, dict):
                raise RuntimeError("child Agent interrupt payload is invalid")
            native_request = child_resume.get("native_request", {})
            action_requests = (
                list(native_request.get("action_requests", ()))
                if isinstance(native_request, dict)
                else []
            )
            return CapabilitySuspension(
                {
                    "kind": "delegated_agent_approval",
                    "action_requests": action_requests,
                    "child_run_id": result.run_id,
                    "child_interrupt_id": str(child_interrupt.get("id") or ""),
                    "child_resume": child_resume,
                }
            )
        return result.to_capability_result()

    return CapabilitySpec(
        name=cap_name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "委托给子智能体的任务描述，应包含完整上下文和需求",
                },
            },
            "required": ["task"],
        },
        handler=delegate,
        effect=CapabilityEffect.WRITE,
        permission=f"delegate:{agent_name}",
        timeout_seconds=300,
        source_policy=SourcePolicy.NONE,
    )


def create_delegate_capabilities(coordinator: RunCoordinator) -> list[CapabilitySpec]:
    return [
        _create_delegate_capability(cap_name, agent_name, desc, intent, skill_name, coordinator)
        for cap_name, (agent_name, desc, intent, skill_name) in _SUBAGENTS.items()
    ]
