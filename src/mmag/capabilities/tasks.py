"""Task-tracking capabilities for the organisational item supervisor."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy

if TYPE_CHECKING:
    from ..memory import Memory


def create_create_task_capability(memory: Memory) -> CapabilitySpec:
    async def create_task(
        title: str,
        assignee_id: str = "",
        description: str = "",
        task_type: str = "task",
        due_time: float = 0,
        priority: int = 1,
        channel_id: str = "",
        creator_id: str = "",
    ) -> dict:
        task = memory.repositories.tasks.create({
            "title": title,
            "description": description,
            "type": task_type,
            "assignee_id": assignee_id,
            "creator_id": creator_id,
            "channel_id": channel_id,
            "due_time": due_time,
            "priority": priority,
        })
        return {"status": "ok", "task": _format_task(task)}

    return CapabilitySpec(
        name="create_task",
        description="创建任务。当用户要求创建任务、待办、工单时使用此工具。",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题"},
                "assignee_id": {"type": "string", "description": "负责人 user_id"},
                "description": {"type": "string", "description": "详细描述"},
                "task_type": {"type": "string", "enum": ["task", "meeting", "okr"], "description": "任务类型", "default": "task"},
                "due_time": {"type": "number", "description": "截止时间 Unix 秒, 0=无截止", "default": 0},
                "priority": {"type": "integer", "enum": [0, 1, 2], "description": "0=低 1=中 2=高", "default": 1},
                "channel_id": {"type": "string", "description": "所属频道 ID"},
                "creator_id": {"type": "string", "description": "创建人 user_id"},
            },
            "required": ["title"],
        },
        handler=create_task,
        effect=CapabilityEffect.WRITE,
        permission="task:write",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def create_list_tasks_capability(memory: Memory) -> CapabilitySpec:
    async def list_tasks(
        assignee_id: str = "",
        status: str = "",
        task_type: str = "",
        channel_id: str = "",
        due_before: float = 0,
        limit: int = 20,
    ) -> dict:
        tasks = memory.repositories.tasks.list(
            assignee_id=assignee_id,
            status=status,
            task_type=task_type,
            channel_id=channel_id,
            due_before=due_before,
            limit=min(limit, 50),
        )
        if not tasks:
            return {"count": 0, "tasks": [], "note": "未找到匹配的事项"}
        return {"count": len(tasks), "tasks": [_format_task(t) for t in tasks]}

    return CapabilitySpec(
        name="list_tasks",
        description="查询任务列表。可按负责人、状态、频道、截止时间过滤。",
        input_schema={
            "type": "object",
            "properties": {
                "assignee_id": {"type": "string", "description": "按负责人过滤"},
                "status": {"type": "string", "description": "pending/in_progress/done/cancelled"},
                "task_type": {"type": "string", "description": "task/meeting/okr"},
                "channel_id": {"type": "string", "description": "按频道过滤"},
                "due_before": {"type": "number", "description": "截止时间 <= 此 Unix 秒"},
                "limit": {"type": "integer", "default": 20, "maximum": 50},
            },
            "required": [],
        },
        handler=list_tasks,
        effect=CapabilityEffect.READ,
        permission="task:read",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def create_update_task_capability(memory: Memory) -> CapabilitySpec:
    async def update_task(
        task_id: str,
        status: str = "",
        assignee_id: str = "",
        title: str = "",
        description: str = "",
        due_time: float = 0,
        priority: int = 0,
    ) -> dict:
        updates: dict = {}
        if status:
            updates["status"] = status
        if assignee_id:
            updates["assignee_id"] = assignee_id
        if title:
            updates["title"] = title
        if description:
            updates["description"] = description
        if due_time:
            updates["due_time"] = due_time
        if priority:
            updates["priority"] = priority

        task = memory.repositories.tasks.update(task_id, updates)
        if not task:
            return {"status": "error", "error": f"任务 {task_id} 不存在"}
        return {"status": "ok", "task": _format_task(task)}

    return CapabilitySpec(
        name="update_task",
        description="更新任务状态、负责人、标题、截止时间等。",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "status": {"type": "string", "description": "pending/in_progress/done/cancelled"},
                "assignee_id": {"type": "string", "description": "新的负责人"},
                "title": {"type": "string", "description": "新标题"},
                "description": {"type": "string", "description": "新描述"},
                "due_time": {"type": "number", "description": "新截止时间 Unix 秒"},
                "priority": {"type": "integer", "description": "0-低 1-中 2-高"},
            },
            "required": ["task_id"],
        },
        handler=update_task,
        effect=CapabilityEffect.WRITE,
        permission="task:write",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def create_get_task_overview_capability(memory: Memory) -> CapabilitySpec:
    async def get_task_overview(channel_id: str = "") -> dict:
        counts = memory.repositories.tasks.overview(channel_id=channel_id)
        now = time.time()
        overdue = memory.repositories.tasks.list(
            status="pending", due_before=now, channel_id=channel_id, limit=50
        )
        return {
            "status_counts": counts,
            "overdue_count": len(overdue),
            "overdue_tasks": [_format_task(t) for t in overdue[:10]],
        }

    return CapabilitySpec(
        name="get_task_overview",
        description="获取任务总览：各状态数量统计和逾期任务列表。",
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "按频道过滤 (留空=全部)"},
            },
            "required": [],
        },
        handler=get_task_overview,
        effect=CapabilityEffect.READ,
        permission="task:read",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def _format_task(task: dict) -> dict:
    return {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "type": task.get("type", "task"),
        "status": task.get("status", "pending"),
        "assignee_id": task.get("assignee_id", ""),
        "creator_id": task.get("creator_id", ""),
        "channel_id": task.get("channel_id", ""),
        "source": task.get("source", "manual"),
        "due_time": task.get("due_time", 0),
        "priority": task.get("priority", 1),
        "created_at": task.get("created_at", 0),
        "updated_at": task.get("updated_at", 0),
    }


def create_task_capabilities(memory: Memory) -> list[CapabilitySpec]:
    return [
        create_create_task_capability(memory),
        create_list_tasks_capability(memory),
        create_update_task_capability(memory),
        create_get_task_overview_capability(memory),
    ]
