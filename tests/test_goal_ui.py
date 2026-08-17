"""BDD-style acceptance tests for the native Mattermost Goal surface."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.agent_system import AgentOutput
from mmag.application import (
    ActionClaims,
    ActionTokenError,
    ActionTokenService,
    BotIdentity,
    GoalWorkspaceUI,
    MattermostRenderer,
    MessageHandler,
    ResponseKind,
    ResponseView,
    RunStatus,
)
from mmag.capabilities import CapabilityResult, CapabilityStatus, get_capability_context
from mmag.control_plane import Scope, ScopeKind, SQLiteControlPlane
from mmag.governance import get_governance_context
from mmag.runtimes import AgentResult


def _scope() -> Scope:
    return Scope(
        id="mattermost:installation-1:tenant-1:chn:channel-1",
        organization_id="tenant-1",
        conversation_id="channel-1",
        platform="mattermost",
        installation_id="installation-1",
        tenant_id="tenant-1",
        kind=ScopeKind.CHANNEL,
        team_id="team-1",
        channel_type="O",
    )


def _goal(status: str = "active") -> dict:
    return {
        "id": "goal_internal_123",
        "title": "提升发布稳定性",
        "description": "",
        "status": status,
        "owner_id": "owner-1",
        "due_time": 1_789_092_000_000,
        "success_criteria": [
            {
                "description": "完成一个关联任务",
                "current_value": None,
                "target_value": 1,
                "unit": "个",
            }
        ],
    }


def _ui(tmp_path, *, registry=None, guard=None):
    control = SQLiteControlPlane(str(tmp_path / "goal-ui.db"))
    tokens = ActionTokenService(
        "goal-ui-test-signing-secret-32-bytes",
        control,
        owner_id="bot-1",
    )
    resolver = MagicMock()
    resolver.resolve_post.return_value = _scope()
    mm = MagicMock()
    mm.get_username.return_value = "alice"
    ui = GoalWorkspaceUI(
        mm_client=mm,
        capability_registry=registry or MagicMock(),
        access_guard=guard or MagicMock(require=AsyncMock()),
        scope_resolver=resolver,
        action_tokens=tokens,
        audit_store=MagicMock(),
        policy_ref="project@1.2.0",
        allowed_capabilities=(
            "create_goal",
            "list_goals",
            "update_goal",
            "get_goal_overview",
        ),
    )
    return ui, control, tokens


def test_given_created_goal_then_show_compact_native_card_without_technical_ids(tmp_path):
    ui, control, _ = _ui(tmp_path)
    output = AgentOutput(
        text="verbose model response",
        agent_name="project",
        runtime_result=AgentResult(
            text="verbose model response",
            runtime="deepagents",
            capability_calls=(
                {
                    "name": "create_goal",
                    "status": "success",
                    "result": {"status": "ok", "goal": _goal()},
                },
            ),
        ),
    )
    fallback = ResponseView(
        ResponseKind.RESULT,
        "项目计划",
        "verbose model response",
        RunStatus.SUCCEEDED,
        run_id="mattermost:post-1",
    )

    view = ui.attach(
        {"id": "post-1", "channel_id": "channel-1", "user_id": "user-1"},
        output,
        fallback,
    )
    rendered = MattermostRenderer(
        action_callback_url="https://mmag.example.com/actions"
    ).render(view)
    markdown = rendered.chunks[0]

    assert view.title == "目标已创建"
    assert markdown.startswith("### 🎯 目标已创建")
    assert "提升发布稳定性" in markdown
    assert "负责人 alice" in markdown
    assert "截止 2026-09-11 10:00" in markdown
    assert "#### 概况" not in markdown
    assert "成功标准：完成一个关联任务" in markdown
    assert "goal_internal_123" not in markdown
    assert "Run：" not in markdown
    assert rendered.props["mmag_run_id"] == "mattermost:post-1"
    assert {action.action for action in view.actions} == {
        "goal_view",
        "goal_complete",
        "goal_cancel",
    }
    assert all("confirm" not in action for action in rendered.actions)
    assert "text" not in rendered.props["attachments"][0]
    control.close()


@pytest.mark.asyncio
async def test_given_goal_action_then_execute_through_project_policy_and_trusted_scope(tmp_path):
    observed = {}
    registry = MagicMock()

    async def execute(name, arguments):
        observed["name"] = name
        observed["arguments"] = arguments
        observed["capability"] = get_capability_context()
        observed["governance"] = get_governance_context()
        return CapabilityResult(
            CapabilityStatus.SUCCESS,
            data={"status": "ok", "goal": _goal("completed")},
        )

    registry.execute = execute
    guard = MagicMock()
    guard.require = AsyncMock()
    ui, control, _ = _ui(tmp_path, registry=registry, guard=guard)
    claims = ActionClaims(
        issuer="bot-1",
        jti="action-1",
        action="goal_complete_confirm",
        target="goal_internal_123",
        scope_id=_scope().id,
        run_id="mattermost:post-1",
        conversation_id="channel-1",
        root_id="post-1",
        requested_by="user-1",
        expires_at=9_999_999_999,
    )

    view = await ui.handle_action(claims, actor_id="user-2", scope=_scope())

    assert observed["name"] == "update_goal"
    assert observed["arguments"] == {
        "goal_id": "goal_internal_123",
        "status": "completed",
    }
    assert observed["capability"].actor_id == "user-2"
    assert observed["capability"].scope == _scope().id
    assert observed["governance"].policy_ref == "project@1.2.0"
    assert "update_goal" in observed["governance"].allowed_capabilities
    assert view.title == "目标已完成"
    assert view.actions == ()
    guard.require.assert_awaited_once_with(
        "user-2", _scope().id, channel_id="channel-1"
    )
    control.close()


@pytest.mark.asyncio
async def test_given_first_terminal_click_then_show_native_confirmation_without_write(tmp_path):
    observed = []
    registry = MagicMock()

    async def execute(name, arguments):
        observed.append((name, arguments))
        return CapabilityResult(
            CapabilityStatus.SUCCESS,
            data={"status": "ok", "goal": _goal(), "task_count": 0},
        )

    registry.execute = execute
    ui, control, _ = _ui(tmp_path, registry=registry)
    claims = ActionClaims(
        issuer="bot-1",
        jti="action-confirm-1",
        action="goal_cancel",
        target="goal_internal_123",
        scope_id=_scope().id,
        run_id="mattermost:post-1",
        conversation_id="channel-1",
        root_id="post-1",
        requested_by="user-1",
        expires_at=9_999_999_999,
    )

    view = await ui.handle_action(claims, actor_id="user-1", scope=_scope())

    assert observed == [("get_goal_overview", {"goal_id": "goal_internal_123"})]
    assert view.title == "确认取消目标"
    assert view.status is RunStatus.WAITING_APPROVAL
    rendered = MattermostRenderer(
        action_callback_url="https://mmag.example.com/actions"
    ).render(view)
    assert "#### 操作影响" not in rendered.chunks[0]
    assert "取消后不能重新激活。" in rendered.chunks[0]
    assert {action.action for action in view.actions} == {
        "goal_cancel_confirm",
        "goal_view",
    }
    control.close()


@pytest.mark.asyncio
async def test_given_signed_goal_button_then_callback_replaces_post_with_native_view(tmp_path):
    control = SQLiteControlPlane(str(tmp_path / "callback.db"))
    tokens = ActionTokenService(
        "goal-ui-test-signing-secret-32-bytes",
        control,
        owner_id="bot-1",
    )
    token = tokens.issue(
        action="goal_view",
        target="goal_internal_123",
        scope_id=_scope().id,
        run_id="mattermost:post-1",
        conversation_id="channel-1",
        root_id="post-1",
        requested_by="user-1",
    )
    handler = MessageHandler.__new__(MessageHandler)
    handler.action_tokens = tokens
    handler.identity = BotIdentity("bot-1", "hz_bot")
    handler.mm = MagicMock()
    handler.mm.get_post_async = AsyncMock(
        return_value={
            "id": "bot-post-1",
            "channel_id": "channel-1",
            "user_id": "bot-1",
            "message": "old",
        }
    )
    handler.scope_resolver = MagicMock()
    handler.scope_resolver.resolve_post.return_value = _scope()
    handler.post_scope = MagicMock(return_value=_scope().id)
    handler.goal_ui = MagicMock()
    handler.goal_ui.handle_action = AsyncMock(
        return_value=ResponseView(
            ResponseKind.RESULT,
            "目标进度",
            "提升发布稳定性",
            RunStatus.SUCCEEDED,
            run_id="mattermost:post-1",
        )
    )
    handler.delivery = MagicMock()
    handler.delivery.renderer = MattermostRenderer(
        action_callback_url="https://mmag.example.com/actions"
    )

    response = await handler.handle_action_callback(
        {
            "post_id": "bot-post-1",
            "channel_id": "channel-1",
            "user_id": "user-2",
            "context": {"token": token},
        }
    )

    assert "目标进度" in response["update"]["message"]
    assert response["update"]["props"]["mmag_run_id"] == "mattermost:post-1"
    assert "ephemeral_text" not in response
    handler.goal_ui.handle_action.assert_awaited_once()
    with pytest.raises(ActionTokenError, match="already used"):
        tokens.consume(token, actor_id="user-2")
    control.close()
