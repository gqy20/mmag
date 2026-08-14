"""Scope-bound task-tracking capabilities for the project agent."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy
from .context import CapabilityContext, get_capability_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..control_plane import MattermostAccessGuard
    from ..memory import Memory

_TASK_TYPES = frozenset({"task", "meeting", "okr"})
_TASK_STATUSES = frozenset({"pending", "in_progress", "done", "cancelled"})


def _error(message: str) -> dict[str, str]:
    return {"status": "error", "error": message}


def _trusted_context(
    memory: Memory,
    context_provider: Callable[[], CapabilityContext | None],
) -> CapabilityContext:
    context = context_provider()
    if (
        context is None
        or not context.actor_id
        or not context.conversation_id
        or not context.scope
    ):
        raise PermissionError("缺少可信的任务访问上下文")
    if context.installation_id and context.installation_id != memory.installation_id:
        raise PermissionError("任务上下文属于其他安装")
    if context.tenant_id and context.tenant_id != memory.tenant_id:
        raise PermissionError("任务上下文属于其他租户")
    return context


async def _authorize_context(
    memory: Memory,
    context_provider: Callable[[], CapabilityContext | None],
    access_guard: MattermostAccessGuard | None,
) -> CapabilityContext:
    context = _trusted_context(memory, context_provider)
    if access_guard is None:
        raise PermissionError("任务访问校验器未配置")
    await access_guard.require(
        context.actor_id,
        context.scope,
        channel_id=context.conversation_id,
    )
    return context


async def _authorize_assignee(mm_client: Any, context: CapabilityContext, assignee_id: str) -> None:
    if not assignee_id or assignee_id == context.actor_id:
        return
    if mm_client is None:
        raise PermissionError("无法校验任务负责人")
    try:
        member = await mm_client.get_channel_member_async(
            context.conversation_id,
            assignee_id,
        )
    except Exception as error:
        raise PermissionError("任务负责人不属于当前会话") from error
    if str(member.get("user_id") or "") != assignee_id:
        raise PermissionError("任务负责人不属于当前会话")


def _execution_key(context: CapabilityContext, arguments: dict[str, Any]) -> str:
    if context.execution_key:
        return context.execution_key
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    identity = "\0".join(
        (
            context.installation_id,
            context.tenant_id,
            context.scope,
            context.actor_id,
            context.run_id,
            context.message_id,
            "create_task",
            payload,
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def create_create_task_capability(
    memory: Memory,
    mm_client: Any = None,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
) -> CapabilitySpec:
    async def create_task(
        title: str,
        assignee_id: str = "",
        description: str = "",
        task_type: str = "task",
        due_time: float = 0,
        priority: int = 1,
    ) -> dict:
        if not title.strip():
            return _error("任务标题不能为空")
        if task_type not in _TASK_TYPES:
            return _error("任务类型无效")
        if due_time < 0 or priority not in {0, 1, 2}:
            return _error("截止时间或优先级无效")
        try:
            context = await _authorize_context(memory, context_provider, access_guard)
            await _authorize_assignee(mm_client, context, assignee_id)
        except PermissionError as error:
            return _error(str(error))
        arguments = {
            "title": title,
            "assignee_id": assignee_id,
            "description": description,
            "task_type": task_type,
            "due_time": due_time,
            "priority": priority,
        }
        task = memory.repositories.tasks.create({
            "title": title.strip(),
            "description": description,
            "type": task_type,
            "assignee_id": assignee_id,
            "creator_id": context.actor_id,
            "channel_id": context.conversation_id,
            "scope_id": context.scope,
            "execution_key": _execution_key(context, arguments),
            "due_time": due_time,
            "priority": priority,
        })
        return {"status": "ok", "task": _format_task(task)}

    return CapabilitySpec(
        name="create_task",
        description="在当前可信 Scope 中创建任务。当用户要求创建任务、待办、工单时使用。",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "assignee_id": {"type": "string", "description": "负责人 user_id"},
                "description": {"type": "string", "maxLength": 10000},
                "task_type": {"type": "string", "enum": sorted(_TASK_TYPES), "default": "task"},
                "due_time": {"type": "number", "minimum": 0, "default": 0},
                "priority": {"type": "integer", "enum": [0, 1, 2], "default": 1},
            },
            "required": ["title"],
        },
        handler=create_task,
        effect=CapabilityEffect.WRITE,
        permission="task:write",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def create_list_tasks_capability(
    memory: Memory,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
) -> CapabilitySpec:
    async def list_tasks(
        assignee_id: str = "",
        status: str = "",
        task_type: str = "",
        due_before: float = 0,
        limit: int = 20,
    ) -> dict:
        if status and status not in _TASK_STATUSES:
            return _error("任务状态无效")
        if task_type and task_type not in _TASK_TYPES:
            return _error("任务类型无效")
        try:
            context = await _authorize_context(memory, context_provider, access_guard)
        except PermissionError as error:
            return _error(str(error))
        tasks = memory.repositories.tasks.list(
            assignee_id=assignee_id,
            status=status,
            task_type=task_type,
            scope_id=context.scope,
            due_before=max(due_before, 0),
            limit=max(1, min(limit, 50)),
        )
        return {"count": len(tasks), "tasks": [_format_task(task) for task in tasks]}

    return CapabilitySpec(
        name="list_tasks",
        description="查询当前可信 Scope 中的任务列表。",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "assignee_id": {"type": "string"},
                "status": {"type": "string", "enum": sorted(_TASK_STATUSES)},
                "task_type": {"type": "string", "enum": sorted(_TASK_TYPES)},
                "due_before": {"type": "number", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": [],
        },
        handler=list_tasks,
        effect=CapabilityEffect.READ,
        permission="task:read",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def create_update_task_capability(
    memory: Memory,
    mm_client: Any = None,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
) -> CapabilitySpec:
    async def update_task(
        task_id: str,
        status: str | None = None,
        assignee_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        due_time: float | None = None,
        priority: int | None = None,
    ) -> dict:
        if status is not None and status not in _TASK_STATUSES:
            return _error("任务状态无效")
        if title is not None and not title.strip():
            return _error("任务标题不能为空")
        if due_time is not None and due_time < 0:
            return _error("截止时间无效")
        if priority is not None and priority not in {0, 1, 2}:
            return _error("优先级无效")
        try:
            context = await _authorize_context(memory, context_provider, access_guard)
            if assignee_id is not None:
                await _authorize_assignee(mm_client, context, assignee_id)
        except PermissionError as error:
            return _error(str(error))
        updates = {
            key: value
            for key, value in {
                "status": status,
                "assignee_id": assignee_id,
                "title": title.strip() if title is not None else None,
                "description": description,
                "due_time": due_time,
                "priority": priority,
            }.items()
            if value is not None
        }
        task = memory.repositories.tasks.update(task_id, updates, scope_id=context.scope)
        if not task:
            return _error(f"任务 {task_id} 不存在")
        return {"status": "ok", "task": _format_task(task)}

    return CapabilitySpec(
        name="update_task",
        description="更新当前可信 Scope 中的任务。",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": "string", "minLength": 1},
                "status": {"type": "string", "enum": sorted(_TASK_STATUSES)},
                "assignee_id": {"type": "string"},
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "description": {"type": "string", "maxLength": 10000},
                "due_time": {"type": "number", "minimum": 0},
                "priority": {"type": "integer", "enum": [0, 1, 2]},
            },
            "required": ["task_id"],
        },
        handler=update_task,
        effect=CapabilityEffect.WRITE,
        permission="task:write",
        timeout_seconds=10,
        source_policy=SourcePolicy.NONE,
    )


def create_get_task_overview_capability(
    memory: Memory,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
) -> CapabilitySpec:
    async def get_task_overview() -> dict:
        try:
            context = await _authorize_context(memory, context_provider, access_guard)
        except PermissionError as error:
            return _error(str(error))
        counts = memory.repositories.tasks.overview(scope_id=context.scope)
        overdue = memory.repositories.tasks.list(
            status="pending",
            due_before=time.time(),
            scope_id=context.scope,
            limit=50,
        )
        return {
            "status_counts": counts,
            "overdue_count": len(overdue),
            "overdue_tasks": [_format_task(task) for task in overdue[:10]],
        }

    return CapabilitySpec(
        name="get_task_overview",
        description="获取当前可信 Scope 的任务状态统计和逾期列表。",
        input_schema={"type": "object", "additionalProperties": False, "properties": {}, "required": []},
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


def create_task_capabilities(
    memory: Memory,
    mm_client: Any = None,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
) -> list[CapabilitySpec]:
    return [
        create_create_task_capability(
            memory,
            mm_client,
            context_provider=context_provider,
            access_guard=access_guard,
        ),
        create_list_tasks_capability(
            memory,
            context_provider=context_provider,
            access_guard=access_guard,
        ),
        create_update_task_capability(
            memory,
            mm_client,
            context_provider=context_provider,
            access_guard=access_guard,
        ),
        create_get_task_overview_capability(
            memory,
            context_provider=context_provider,
            access_guard=access_guard,
        ),
    ]
