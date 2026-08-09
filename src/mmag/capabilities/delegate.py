"""Delegate capabilities that let mmchat dispatch to sub-agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent_system import AgentRequest
from ..logger import get_logger, log_context, log_event
from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

if TYPE_CHECKING:
    from ..agent_system import AgentRegistry

log = get_logger(__name__)

_SUBAGENTS: dict[str, tuple[str, str, str]] = {
    # name → (agent_name, description, intent)
    "delegate_ppt": (
        "ppt",
        "将 PPT/幻灯片/演示文稿生成任务委托给 PPT 子智能体。"
        "子智能体拥有独立的 workspace 和 slides 技能，会生成 pptx 文件并交付。"
        "当用户需要制作 PPT、幻灯片、演示文稿时使用。",
        "presentation",
    ),
    "delegate_report": (
        "report",
        "将研究报告/会议纪要/调研报告生成任务委托给 Report 子智能体。"
        "子智能体会搜索消息和知识库、分析链接，生成结构化报告。"
        "当用户需要研报、会议纪要、调研报告时使用。",
        "report",
    ),
    "delegate_project": (
        "project",
        "将项目计划/任务拆解/里程碑整理任务委托给 Project 子智能体。"
        "当用户需要项目计划、任务拆解、进度跟踪时使用。",
        "project",
    ),
    "delegate_link": (
        "link",
        "将链接分析任务委托给 Link 子智能体。"
        "当用户需要分析、总结某个 URL 的内容时使用。",
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

        request = AgentRequest(
            intent=intent,
            prompt=task,
            actor_id=log_context.get("actor_id", "mmchat"),
            task_id=log_context.get("trace_id", ""),
            run_id=log_context.get("run_id", ""),
        )

        log_event(log, "delegate.started", status="running",
                   subagent=agent_name, task_preview=task[:100])

        try:
            output = await agent.run(request)
        except Exception as error:
            log_event(log, "delegate.failed", level=40, status="failed",
                       subagent=agent_name, error=str(error)[:200])
            return {"status": "error", "error": f"子智能体执行失败: {error}"}

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
