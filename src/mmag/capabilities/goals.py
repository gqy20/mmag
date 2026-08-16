"""Governed capabilities for the deliberately small Goal product object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import CapabilityEffect, CapabilitySpec, SourcePolicy
from .context import CapabilityContext, get_capability_context
from .tasks import authorize_work_context, authorize_work_owner, work_execution_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..control_plane import MattermostAccessGuard
    from ..memory import Memory

_GOAL_STATUSES = frozenset({"draft", "active", "completed", "cancelled"})
_GOAL_TRANSITIONS = {
    "draft": frozenset({"active", "cancelled"}),
    "active": frozenset({"completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

_CRITERION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string", "minLength": 1, "maxLength": 500},
        "current_value": {"type": ["number", "null"]},
        "target_value": {"type": ["number", "null"]},
        "unit": {"type": "string", "maxLength": 50},
    },
    "required": ["description"],
}


def _error(message: str) -> dict[str, str]:
    return {"status": "error", "error": message}


def _criteria(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "description": str(item["description"]).strip(),
            "current_value": item.get("current_value"),
            "target_value": item.get("target_value"),
            "unit": str(item.get("unit") or "").strip(),
        }
        for item in value
    ]


def _format_goal(goal: dict) -> dict:
    return {
        "id": goal.get("id", ""),
        "title": goal.get("title", ""),
        "description": goal.get("description", ""),
        "status": goal.get("status", "active"),
        "owner_id": goal.get("owner_id", ""),
        "creator_id": goal.get("creator_id", ""),
        "channel_id": goal.get("channel_id", ""),
        "start_time": goal.get("start_time", 0),
        "due_time": goal.get("due_time", 0),
        "success_criteria": goal.get("success_criteria", []),
        "source_refs": goal.get("source_refs", []),
        "created_at": goal.get("created_at", 0),
        "updated_at": goal.get("updated_at", 0),
    }


def create_goal_capabilities(
    memory: Memory,
    mm_client: Any = None,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
) -> list[CapabilitySpec]:
    async def create_goal(
        title: str,
        owner_id: str = "",
        description: str = "",
        status: str = "active",
        start_time: float = 0,
        due_time: float = 0,
        success_criteria: list[dict[str, Any]] | None = None,
    ) -> dict:
        if not title.strip():
            return _error("目标标题不能为空")
        if status not in {"draft", "active"}:
            return _error("新目标只能是 draft 或 active")
        if start_time < 0 or due_time < 0 or (start_time and due_time and due_time < start_time):
            return _error("目标时间范围无效")
        criteria = _criteria(success_criteria or [])
        if len(criteria) > 20 or any(not item["description"] for item in criteria):
            return _error("验收条件无效")
        try:
            context = await authorize_work_context(memory, context_provider, access_guard)
            await authorize_work_owner(mm_client, context, owner_id)
        except PermissionError as error:
            return _error(str(error))
        arguments = {
            "title": title,
            "owner_id": owner_id,
            "description": description,
            "status": status,
            "start_time": start_time,
            "due_time": due_time,
            "success_criteria": criteria,
        }
        goal = memory.repositories.goals.create(
            {
                "title": title.strip(),
                "description": description,
                "status": status,
                "owner_id": owner_id,
                "creator_id": context.actor_id,
                "channel_id": context.conversation_id,
                "scope_id": context.scope,
                "start_time": start_time,
                "due_time": due_time,
                "success_criteria": criteria,
                "source_refs": (
                    [f"mattermost_post:{context.message_id}"] if context.message_id else []
                ),
                "execution_key": work_execution_key(
                    context, arguments, capability="create_goal"
                ),
            }
        )
        return {"status": "ok", "goal": _format_goal(goal)}

    async def list_goals(
        status: str = "", owner_id: str = "", limit: int = 20
    ) -> dict:
        if status and status not in _GOAL_STATUSES:
            return _error("目标状态无效")
        try:
            context = await authorize_work_context(memory, context_provider, access_guard)
        except PermissionError as error:
            return _error(str(error))
        goals = memory.repositories.goals.list(
            scope_id=context.scope,
            status=status,
            owner_id=owner_id,
            limit=max(1, min(limit, 50)),
        )
        return {"count": len(goals), "goals": [_format_goal(goal) for goal in goals]}

    async def update_goal(
        goal_id: str,
        status: str | None = None,
        owner_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        start_time: float | None = None,
        due_time: float | None = None,
        success_criteria: list[dict[str, Any]] | None = None,
    ) -> dict:
        if title is not None and not title.strip():
            return _error("目标标题不能为空")
        if (start_time is not None and start_time < 0) or (
            due_time is not None and due_time < 0
        ):
            return _error("目标时间范围无效")
        criteria = _criteria(success_criteria) if success_criteria is not None else None
        if criteria is not None and (
            len(criteria) > 20 or any(not item["description"] for item in criteria)
        ):
            return _error("验收条件无效")
        try:
            context = await authorize_work_context(memory, context_provider, access_guard)
            if owner_id is not None:
                await authorize_work_owner(mm_client, context, owner_id)
        except PermissionError as error:
            return _error(str(error))
        existing = memory.repositories.goals.get(goal_id, scope_id=context.scope)
        if existing is None:
            return _error(f"目标 {goal_id} 不存在")
        if status is not None:
            if status not in _GOAL_STATUSES:
                return _error("目标状态无效")
            current = str(existing.get("status") or "active")
            if status != current and status not in _GOAL_TRANSITIONS[current]:
                return _error(f"目标状态不能从 {current} 变为 {status}")
        next_start = existing["start_time"] if start_time is None else start_time
        next_due = existing["due_time"] if due_time is None else due_time
        if next_start and next_due and next_due < next_start:
            return _error("目标时间范围无效")
        goal = memory.repositories.goals.update(
            goal_id,
            {
                "status": status,
                "owner_id": owner_id,
                "title": title.strip() if title is not None else None,
                "description": description,
                "start_time": start_time,
                "due_time": due_time,
                "success_criteria": criteria,
            },
            scope_id=context.scope,
        )
        return {"status": "ok", "goal": _format_goal(goal or {})}

    async def get_goal_overview(goal_id: str) -> dict:
        try:
            context = await authorize_work_context(memory, context_provider, access_guard)
        except PermissionError as error:
            return _error(str(error))
        goal = memory.repositories.goals.get(goal_id, scope_id=context.scope)
        if goal is None:
            return _error(f"目标 {goal_id} 不存在")
        tasks = memory.repositories.tasks.list(
            goal_id=goal_id, scope_id=context.scope, limit=50
        )
        counts: dict[str, int] = {}
        for task in tasks:
            task_status = str(task.get("status") or "pending")
            counts[task_status] = counts.get(task_status, 0) + 1
        completed = counts.get("done", 0)
        return {
            "goal": _format_goal(goal),
            "task_counts": counts,
            "task_count": len(tasks),
            "completed_task_count": completed,
            "completion_ratio": completed / len(tasks) if tasks else 0,
            "tasks": [
                {
                    "id": task.get("id", ""),
                    "title": task.get("title", ""),
                    "status": task.get("status", "pending"),
                    "assignee_id": task.get("assignee_id", ""),
                    "due_time": task.get("due_time", 0),
                }
                for task in tasks
            ],
        }

    common_properties = {
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "owner_id": {"type": "string", "description": "当前会话成员 user_id"},
        "description": {"type": "string", "maxLength": 10000},
        "start_time": {"type": "number", "minimum": 0},
        "due_time": {"type": "number", "minimum": 0},
        "success_criteria": {
            "type": "array",
            "maxItems": 20,
            "items": _CRITERION_SCHEMA,
        },
    }
    return [
        CapabilitySpec(
            name="create_goal",
            description=(
                "在当前可信 Scope 创建正式目标。只在用户明确创建或确认候选目标时使用。"
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **common_properties,
                    "status": {"type": "string", "enum": ["draft", "active"], "default": "active"},
                },
                "required": ["title"],
            },
            handler=create_goal,
            effect=CapabilityEffect.WRITE,
            permission="goal:write",
            timeout_seconds=10,
            source_policy=SourcePolicy.NONE,
        ),
        CapabilitySpec(
            name="list_goals",
            description="查询当前可信 Scope 中的目标。",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"type": "string", "enum": sorted(_GOAL_STATUSES)},
                    "owner_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
                "required": [],
            },
            handler=list_goals,
            effect=CapabilityEffect.READ,
            permission="goal:read",
            timeout_seconds=10,
            source_policy=SourcePolicy.NONE,
        ),
        CapabilitySpec(
            name="update_goal",
            description="更新当前可信 Scope 中的目标内容或单向生命周期状态。",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "goal_id": {"type": "string", "minLength": 1},
                    **common_properties,
                    "status": {"type": "string", "enum": sorted(_GOAL_STATUSES)},
                },
                "required": ["goal_id"],
            },
            handler=update_goal,
            effect=CapabilityEffect.WRITE,
            permission="goal:write",
            timeout_seconds=10,
            source_policy=SourcePolicy.NONE,
        ),
        CapabilitySpec(
            name="get_goal_overview",
            description="获取一个目标及其关联任务的确定性进展摘要。",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"goal_id": {"type": "string", "minLength": 1}},
                "required": ["goal_id"],
            },
            handler=get_goal_overview,
            effect=CapabilityEffect.READ,
            permission="goal:read",
            timeout_seconds=10,
            source_policy=SourcePolicy.NONE,
        ),
    ]
