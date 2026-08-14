from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.capabilities import CapabilityContext, create_task_capabilities
from mmag.memory import Memory


def _context(scope: str, *, execution_key: str = "execution-1") -> CapabilityContext:
    return CapabilityContext(
        trace_id="trace-1",
        actor_id="actor-1",
        conversation_id="channel-1",
        message_id="message-1",
        message="创建任务",
        scope=scope,
        run_id="run-1",
        installation_id="installation-1",
        tenant_id="tenant-1",
        execution_key=execution_key,
    )


def _capabilities(memory, current_context, *, client=None, guard=None):
    specs = create_task_capabilities(
        memory,
        client,
        context_provider=lambda: current_context[0],
        access_guard=guard,
    )
    return {spec.name: spec for spec in specs}


@pytest.mark.asyncio
async def test_create_task_binds_trusted_context_and_is_idempotent(tmp_path):
    memory = Memory(
        str(tmp_path / "tasks.db"),
        installation_id="installation-1",
        tenant_id="tenant-1",
    )
    current_context = [_context("scope-1")]
    guard = MagicMock()
    guard.require = AsyncMock()
    capabilities = _capabilities(memory, current_context, guard=guard)

    first = await capabilities["create_task"].handler(title="发布版本")
    second = await capabilities["create_task"].handler(title="发布版本")

    assert first["task"]["id"] == second["task"]["id"]
    assert first["task"]["creator_id"] == "actor-1"
    assert first["task"]["channel_id"] == "channel-1"
    assert len(memory.repositories.tasks.list(scope_id="scope-1")) == 1
    assert "creator_id" not in capabilities["create_task"].input_schema["properties"]
    assert "channel_id" not in capabilities["create_task"].input_schema["properties"]
    guard.require.assert_awaited_with("actor-1", "scope-1", channel_id="channel-1")
    memory.close()


@pytest.mark.asyncio
async def test_task_reads_and_updates_are_scope_isolated_and_accept_zero_values(tmp_path):
    memory = Memory(
        str(tmp_path / "tasks.db"),
        installation_id="installation-1",
        tenant_id="tenant-1",
    )
    current_context = [_context("scope-1")]
    guard = MagicMock()
    guard.require = AsyncMock()
    capabilities = _capabilities(memory, current_context, guard=guard)
    created = await capabilities["create_task"].handler(
        title="里程碑",
        priority=2,
        due_time=100,
    )
    task_id = created["task"]["id"]

    current_context[0] = _context("scope-2", execution_key="execution-2")
    hidden = await capabilities["list_tasks"].handler()
    denied = await capabilities["update_task"].handler(task_id=task_id, status="done")
    current_context[0] = _context("scope-1", execution_key="execution-3")
    updated = await capabilities["update_task"].handler(
        task_id=task_id,
        priority=0,
        due_time=0,
        description="",
    )

    assert hidden == {"count": 0, "tasks": []}
    assert denied["status"] == "error"
    assert updated["task"]["priority"] == 0
    assert updated["task"]["due_time"] == 0
    assert updated["task"]["description"] == ""
    memory.close()


@pytest.mark.asyncio
async def test_task_capabilities_fail_closed_for_untrusted_context_and_assignee(tmp_path):
    memory = Memory(
        str(tmp_path / "tasks.db"),
        installation_id="installation-1",
        tenant_id="tenant-1",
    )
    current_context = [None]
    guard = MagicMock()
    guard.require = AsyncMock()
    client = MagicMock()
    client.get_channel_member_async = AsyncMock(return_value={"user_id": "someone-else"})
    capabilities = _capabilities(memory, current_context, client=client, guard=guard)

    missing = await capabilities["create_task"].handler(title="不能创建")
    current_context[0] = _context("scope-1")
    invalid_assignee = await capabilities["create_task"].handler(
        title="不能分配",
        assignee_id="actor-2",
    )

    assert missing["status"] == "error"
    assert invalid_assignee["status"] == "error"
    assert memory.repositories.tasks.list(scope_id="scope-1") == []
    memory.close()
