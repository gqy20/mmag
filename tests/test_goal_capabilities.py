"""BDD-style acceptance tests for the small internal Goal lifecycle."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.capabilities import CapabilityContext, create_goal_capabilities, create_task_capabilities
from mmag.memory import Memory


def _context(scope: str, *, execution_key: str = "goal-execution-1") -> CapabilityContext:
    return CapabilityContext(
        trace_id="trace-goal",
        actor_id="actor-1",
        conversation_id="channel-1",
        message_id="message-1",
        message="创建目标",
        scope=scope,
        run_id="run-goal",
        installation_id="installation-1",
        tenant_id="tenant-1",
        execution_key=execution_key,
    )


def _setup(tmp_path):
    memory = Memory(
        str(tmp_path / "goals.db"),
        installation_id="installation-1",
        tenant_id="tenant-1",
    )
    current_context = [_context("scope-1")]
    guard = MagicMock()
    guard.require = AsyncMock()
    client = MagicMock()
    client.get_channel_member_async = AsyncMock(
        side_effect=lambda _channel, user: {"user_id": user}
    )
    goals = {
        spec.name: spec
        for spec in create_goal_capabilities(
            memory,
            client,
            context_provider=lambda: current_context[0],
            access_guard=guard,
        )
    }
    tasks = {
        spec.name: spec
        for spec in create_task_capabilities(
            memory,
            client,
            context_provider=lambda: current_context[0],
            access_guard=guard,
        )
    }
    return memory, current_context, goals, tasks


@pytest.mark.asyncio
async def test_given_explicit_goal_when_created_then_it_is_idempotent_and_sourced(tmp_path):
    memory, _, goals, _ = _setup(tmp_path)

    first = await goals["create_goal"].handler(
        title="提升产品稳定性",
        owner_id="actor-1",
        success_criteria=[
            {
                "description": "P1 故障为 0",
                "current_value": 2,
                "target_value": 0,
                "unit": "次",
            }
        ],
    )
    second = await goals["create_goal"].handler(
        title="提升产品稳定性",
        owner_id="actor-1",
        success_criteria=[
            {
                "description": "P1 故障为 0",
                "current_value": 2,
                "target_value": 0,
                "unit": "次",
            }
        ],
    )

    assert first["goal"]["id"] == second["goal"]["id"]
    assert first["goal"]["status"] == "active"
    assert first["goal"]["source_refs"] == ["mattermost_post:message-1"]
    assert first["goal"]["success_criteria"][0]["target_value"] == 0
    assert len(memory.repositories.goals.list(scope_id="scope-1")) == 1
    assert "creator_id" not in goals["create_goal"].input_schema["properties"]
    memory.close()


@pytest.mark.asyncio
async def test_given_other_scope_or_nonmember_when_accessing_goal_then_fail_closed(tmp_path):
    memory, current_context, goals, _ = _setup(tmp_path)
    created = await goals["create_goal"].handler(title="不可跨 Scope 目标")

    current_context[0] = _context("scope-2", execution_key="goal-execution-2")
    hidden = await goals["list_goals"].handler()
    missing = await goals["get_goal_overview"].handler(goal_id=created["goal"]["id"])

    assert hidden == {"count": 0, "goals": []}
    assert missing["status"] == "error"
    memory.close()


@pytest.mark.asyncio
async def test_given_unverified_owner_when_creating_goal_then_no_state_is_written(tmp_path):
    memory, _, goals, _ = _setup(tmp_path)
    client = MagicMock()
    client.get_channel_member_async = AsyncMock(return_value={"user_id": "someone-else"})
    current_context = [_context("scope-1")]
    guard = MagicMock()
    guard.require = AsyncMock()
    guarded_goals = {
        spec.name: spec
        for spec in create_goal_capabilities(
            memory,
            client,
            context_provider=lambda: current_context[0],
            access_guard=guard,
        )
    }

    result = await guarded_goals["create_goal"].handler(
        title="must not create", owner_id="actor-2"
    )

    assert result["status"] == "error"
    assert memory.repositories.goals.list(scope_id="scope-1") == []
    memory.close()


@pytest.mark.asyncio
async def test_given_goal_lifecycle_when_terminal_then_it_cannot_be_reopened(tmp_path):
    memory, _, goals, _ = _setup(tmp_path)
    created = await goals["create_goal"].handler(title="可完成目标")
    goal_id = created["goal"]["id"]

    completed = await goals["update_goal"].handler(goal_id=goal_id, status="completed")
    reopened = await goals["update_goal"].handler(goal_id=goal_id, status="active")

    assert completed["goal"]["status"] == "completed"
    assert reopened["status"] == "error"
    assert "completed" in reopened["error"]
    memory.close()


@pytest.mark.asyncio
async def test_given_goal_when_task_is_linked_then_overview_uses_real_task_state(tmp_path):
    memory, current_context, goals, tasks = _setup(tmp_path)
    goal = await goals["create_goal"].handler(title="发布稳定版本")
    goal_id = goal["goal"]["id"]
    current_context[0] = _context("scope-1", execution_key="task-execution-1")
    task = await tasks["create_task"].handler(title="完成回归测试", goal_id=goal_id)
    await tasks["update_task"].handler(task_id=task["task"]["id"], status="done")

    overview = await goals["get_goal_overview"].handler(goal_id=goal_id)

    assert task["task"]["goal_id"] == goal_id
    assert overview["task_counts"] == {"done": 1}
    assert overview["completion_ratio"] == 1
    assert overview["goal"]["status"] == "active"
    memory.close()


@pytest.mark.asyncio
async def test_given_goal_in_other_scope_when_linking_task_then_reject(tmp_path):
    memory, current_context, goals, tasks = _setup(tmp_path)
    goal = await goals["create_goal"].handler(title="scope-1 goal")
    current_context[0] = _context("scope-2", execution_key="task-execution-2")

    task = await tasks["create_task"].handler(
        title="must not link", goal_id=goal["goal"]["id"]
    )

    assert task["status"] == "error"
    assert memory.repositories.tasks.list(scope_id="scope-2") == []
    memory.close()
