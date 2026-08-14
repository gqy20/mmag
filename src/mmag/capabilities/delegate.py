"""Delegate capabilities that let mmchat dispatch to sub-agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_system import AgentRequest
from ..logger import get_logger, log_context, log_event, safe_hash
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy
from .context import get_capability_context

if TYPE_CHECKING:
    from ..agent_system import AgentRegistry

log = get_logger(__name__)

_SUBAGENTS: dict[str, tuple[str, str, str]] = {
    # name → (agent_name, description, intent)
    "delegate_ppt": (
        "ppt",
        "委托 PPT 子智能体生成演示文稿（pptx 文件）。",
        "presentation",
    ),
    "delegate_report": (
        "report",
        "委托 Report 子智能体生成研究报告或会议纪要。",
        "report",
    ),
    "delegate_project": (
        "project",
        "委托 Project 子智能体生成项目计划、里程碑规划或进度简报。",
        "project",
    ),
    "delegate_link": (
        "link",
        "委托 Link 子智能体分析指定 URL 的网页内容。",
        "link",
    ),
}


def _create_delegate_capability(
    cap_name: str,
    agent_name: str,
    description: str,
    intent: str,
    registry: AgentRegistry,
) -> CapabilitySpec:
    async def delegate(task: str) -> dict:
        try:
            agent = registry.get(agent_name)
        except LookupError:
            return {"status": "error", "error": f"子智能体 {agent_name} 未注册"}

        parent_context = get_capability_context()
        if parent_context is None or not parent_context.actor_id or not parent_context.scope:
            return {"status": "error", "error": "缺少可信的子智能体委托上下文"}

        import uuid
        request = AgentRequest(
            intent=intent,
            prompt=task,
            scope=parent_context.scope,
            actor_id=parent_context.actor_id,
            task_id=log_context.get("task_id", parent_context.trace_id),
            run_id=f"delegate:{agent_name}:{uuid.uuid4().hex[:16]}",
        )

        log_event(
            log,
            "delegate.started",
            status="running",
            subagent=agent_name,
            input_sha256=safe_hash(task),
        )

        try:
            output = await agent.run(request)
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

        log_event(log, "delegate.completed", status="completed",
                   subagent=agent_name, text_length=len(output.text))

        return {
            "status": "ok",
            "subagent": agent_name,
            "text": output.text[:5000],
            "artifact_count": len(output.artifacts),
        }

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


def create_delegate_capabilities(registry: AgentRegistry) -> list[CapabilitySpec]:
    return [
        _create_delegate_capability(cap_name, agent_name, desc, intent, registry)
        for cap_name, (agent_name, desc, intent) in _SUBAGENTS.items()
    ]
