from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.agent_system import AgentOutput
from mmag.application import ActionTokenService, ResponsePresenter, TaskDraftCoordinator
from mmag.capabilities import (
    CapabilityBinding,
    CapabilityExecutor,
    CapabilityRegistry,
    create_task_capabilities,
)
from mmag.control_plane import Scope, ScopeKind, SQLiteControlPlane, TaskDraftState
from mmag.memory import Memory


class _UnexpectedAuthorizer:
    def authorize(self, spec, arguments):
        del spec, arguments
        raise AssertionError("human-confirmed draft must use the preauthorized execution path")


class _ScopeResolver:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def resolve_post(self, post):
        del post
        return self.scope


def _meeting_output() -> AgentOutput:
    return AgentOutput(
        text="meeting",
        agent_name="report",
        result={
            "title": "发布会议",
            "summary": "确定发布安排。",
            "decisions": [],
            "action_items": [
                {
                    "content": "整理发布说明",
                    "owner_username": "@alice",
                    "due_date": "周五",
                    "source_post_ids": ["post1"],
                }
            ],
            "open_questions": [],
            "participants": ["alice"],
            "message_range": {
                "type": "thread",
                "message_count": 1,
                "source_post_ids": ["post1"],
            },
            "coverage_notes": [],
        },
    )


def _coordinator(tmp_path):
    path = str(tmp_path / "task-drafts.db")
    memory = Memory(path, installation_id="installation-1", tenant_id="tenant-1")
    control = SQLiteControlPlane(path)
    scope = Scope(
        id="scope-1",
        conversation_id="channel-1",
        platform="mattermost",
        installation_id="installation-1",
        tenant_id="tenant-1",
        kind=ScopeKind.CHANNEL,
        team_id="team-1",
        channel_type="O",
    )
    guard = MagicMock()
    guard.require = AsyncMock()
    registry = CapabilityRegistry()
    executor = CapabilityExecutor(_UnexpectedAuthorizer())
    for spec in create_task_capabilities(memory, access_guard=guard):
        registry.register(CapabilityBinding(spec, executor))
    coordinator = TaskDraftCoordinator(
        store=control.task_drafts,
        memory=memory,
        capability_registry=registry,
        access_guard=guard,
        scope_resolver=_ScopeResolver(scope),
        action_tokens=None,
        audit_store=control,
    )
    return coordinator, memory, control, scope, guard


@pytest.mark.asyncio
async def test_meeting_actions_become_persistent_draft_then_unassigned_tasks(tmp_path):
    coordinator, memory, control, scope, guard = _coordinator(tmp_path)
    coordinator.action_tokens = ActionTokenService(
        "task-draft-test-signing-secret-32-bytes",
        control,
        owner_id="bot-1",
    )
    memory.log_message(
        {
            "id": "post1",
            "channel_id": "channel-1",
            "user_id": "user-2",
            "username": "alice",
            "message": "我周五前整理发布说明",
            "create_at": 1_000,
            "_scope_id": "scope-1",
        }
    )
    post = {
        "id": "request1",
        "root_id": "root1",
        "channel_id": "channel-1",
        "user_id": "requester-1",
    }
    output = _meeting_output()
    view = coordinator.attach(
        post,
        output,
        ResponsePresenter().present(output, run_id="mattermost:request1"),
    )

    draft = control.task_drafts.get_by_run(
        "mattermost:request1",
        installation_id="installation-1",
        tenant_id="tenant-1",
    )
    assert draft.state is TaskDraftState.DRAFT
    assert draft.items[0]["related_person"] == "alice"
    assert "确认任务草案" in view.sections[-1].items[0]
    assert {action.action for action in view.actions} == {
        "task_draft_commit",
        "task_draft_reject",
    }
    claims = coordinator.action_tokens.verify(view.actions[0].token)
    assert claims.target == draft.id
    assert claims.requested_by == "requester-1"

    committed, message = await coordinator.decide(
        draft.id,
        actor_id="requester-1",
        scope=scope,
        approved=True,
    )
    repeated, _ = await coordinator.decide(
        draft.id,
        actor_id="requester-1",
        scope=scope,
        approved=True,
    )

    tasks = memory.repositories.tasks.list(scope_id="scope-1")
    assert committed.state is TaskDraftState.COMMITTED
    assert repeated.task_ids == committed.task_ids
    assert message == "已创建 1 个正式任务，当前均未分配负责人。"
    assert len(tasks) == 1
    assert tasks[0]["assignee_id"] == ""
    assert tasks[0]["type"] == "meeting"
    assert "相关人物：alice" in tasks[0]["description"]
    assert "来源 Post：post1" in tasks[0]["description"]
    assert guard.require.await_count == 2
    control.close()
    memory.close()


@pytest.mark.asyncio
async def test_task_draft_rejects_other_actor_and_unverified_sources(tmp_path):
    coordinator, memory, control, scope, _ = _coordinator(tmp_path)
    post = {
        "id": "request1",
        "channel_id": "channel-1",
        "user_id": "requester-1",
    }
    output = _meeting_output()
    view = coordinator.attach(
        post,
        output,
        ResponsePresenter().present(output, run_id="mattermost:request1"),
    )

    assert "未生成任务草案" in view.warnings[0]
    with pytest.raises(KeyError):
        control.task_drafts.get_by_run(
            "mattermost:request1",
            installation_id="installation-1",
            tenant_id="tenant-1",
        )

    memory.log_message(
        {
            "id": "post1",
            "channel_id": "channel-1",
            "user_id": "user-2",
            "message": "action",
            "create_at": 1_000,
            "_scope_id": "scope-1",
        }
    )
    coordinator.attach(
        post,
        output,
        ResponsePresenter().present(output, run_id="mattermost:request2"),
    )
    draft = control.task_drafts.get_by_run(
        "mattermost:request2",
        installation_id="installation-1",
        tenant_id="tenant-1",
    )
    with pytest.raises(PermissionError, match="创建者"):
        await coordinator.decide(
            draft.id,
            actor_id="other-user",
            scope=scope,
            approved=True,
        )
    assert memory.repositories.tasks.list(scope_id="scope-1") == []
    control.close()
    memory.close()


def test_task_draft_text_command_is_explicit_and_mention_aware():
    draft_id = "a" * 32

    assert TaskDraftCoordinator.parse_command(f"确认任务草案 {draft_id}") == (
        True,
        draft_id,
    )
    assert TaskDraftCoordinator.parse_command(
        f"@mmag 放弃任务草案 {draft_id}", bot_username="mmag"
    ) == (False, draft_id)
    assert TaskDraftCoordinator.parse_command(f"帮我确认任务草案 {draft_id}") is None
