from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.control_plane import (
    AgentRunService,
    AgentRunSpec,
    AgentRunState,
    ApprovalRequest,
    ApprovalService,
    EntityType,
    LangGraphApprovalCoordinator,
    LifecycleService,
    MattermostAccessGuard,
    MattermostApprovalAuthorizer,
    SQLiteControlPlane,
    StaticApprovalAuthorizer,
)
from mmag.runtimes import AgentResult, RuntimeStatus


def _request(requested_by: str = "requester-1") -> ApprovalRequest:
    return ApprovalRequest(
        "approval-1",
        "send_file",
        {},
        "token-1",
        requested_by,
        "mattermost:install-1:tenant-1:chn:channel-1",
    )


@pytest.mark.asyncio
async def test_mattermost_approval_authorizer_rechecks_requester_membership():
    client = AsyncMock()
    client.get_channel_member_async.return_value = {"roles": "channel_user"}
    client.get_user_authorization_async.return_value = {"roles": "system_user"}
    authorizer = MattermostApprovalAuthorizer(client)

    assert await authorizer.can_decide(_request(), "requester-1")
    client.get_channel_member_async.assert_awaited_once_with("channel-1", "requester-1")


@pytest.mark.asyncio
async def test_personal_approval_only_allows_its_owner():
    client = AsyncMock()
    request = ApprovalRequest(
        "approval-1",
        "send_file",
        {},
        "token-1",
        "owner-1",
        "mattermost:install-1:tenant-1:usr:owner-1",
    )
    authorizer = MattermostApprovalAuthorizer(client)

    assert await authorizer.can_decide(request, "owner-1")
    assert not await authorizer.can_decide(request, "system-admin-1")
    client.get_channel_member_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_mattermost_approval_authorizer_requires_admin_role_for_other_actor():
    client = AsyncMock()
    client.get_channel_member_async.return_value = {"roles": "channel_user"}
    client.get_user_authorization_async.return_value = {"roles": "system_user"}
    authorizer = MattermostApprovalAuthorizer(client)

    assert not await authorizer.can_decide(_request(), "member-1")

    client.get_channel_member_async.return_value = {"roles": "channel_user channel_admin"}
    assert await authorizer.can_decide(_request(), "channel-admin-1")


@pytest.mark.asyncio
async def test_mattermost_approval_authorizer_fails_closed_on_identity_error():
    client = AsyncMock()
    client.get_channel_member_async.side_effect = ConnectionError("offline")

    assert not await MattermostApprovalAuthorizer(client).can_decide(_request(), "admin-1")


@pytest.mark.asyncio
async def test_access_guard_denies_removed_channel_member():
    client = AsyncMock()
    client.get_channel_member_async.side_effect = PermissionError("not a member")
    guard = MattermostAccessGuard(
        client,
        installation_id="install-1",
        tenant_id="tenant-1",
    )

    with pytest.raises(PermissionError, match="could not be verified"):
        await guard.require(
            "user-1",
            "mattermost:install-1:tenant-1:chn:channel-1",
            channel_id="channel-1",
        )


@pytest.mark.asyncio
async def test_access_guard_allows_only_personal_scope_owner():
    guard = MattermostAccessGuard(
        AsyncMock(),
        installation_id="install-1",
        tenant_id="tenant-1",
    )
    scope = "mattermost:install-1:tenant-1:usr:user-1"

    await guard.require("user-1", scope)
    with pytest.raises(PermissionError, match="another actor"):
        await guard.require("user-2", scope)


@pytest.mark.asyncio
async def test_access_guard_rejects_personal_delivery_to_shared_channel():
    client = AsyncMock()
    client.get_channel_authorization_async.return_value = {
        "id": "channel-1",
        "type": "O",
    }
    client.get_channel_member_async.return_value = {"user_id": "user-1"}
    guard = MattermostAccessGuard(
        client,
        installation_id="install-1",
        tenant_id="tenant-1",
    )

    with pytest.raises(PermissionError, match="owner's DM"):
        await guard.require(
            "user-1",
            "mattermost:install-1:tenant-1:usr:user-1",
            channel_id="channel-1",
        )


@pytest.mark.asyncio
async def test_checkpoint_resume_stops_after_original_actor_loses_access():
    request = _request()
    request = ApprovalRequest(
        request.id,
        request.capability_name,
        {"interrupt_id": request.resume_token, "thread_id": "run-1"},
        request.resume_token,
        request.requested_by,
        request.scope_id,
    )
    store = MagicMock()
    store.get_approval_request.return_value = request
    authorizer = MagicMock(can_decide=AsyncMock(return_value=True))
    access_guard = MagicMock(require=AsyncMock(side_effect=PermissionError("removed")))
    approvals = MagicMock()
    gateway = MagicMock(resume=AsyncMock())
    coordinator = LangGraphApprovalCoordinator(
        store,
        MagicMock(),
        approvals,
        gateway,
        authorizer=authorizer,
        access_guard=access_guard,
        skill_registry=MagicMock(),
    )

    with pytest.raises(PermissionError, match="removed"):
        await coordinator.resume(
            request.id,
            approved=True,
            actor_id=request.requested_by,
            scope_id=request.scope_id,
            trace_id="trace-1",
        )

    approvals.decide.assert_not_called()
    gateway.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegated_approval_resumes_child_before_parent(tmp_path):
    store = SQLiteControlPlane(tmp_path / "nested-approval.db")
    lifecycle = LifecycleService(store)
    runs = AgentRunService(store, lifecycle)
    scope = "mattermost:install-1:tenant-1:chn:channel-1"
    parent, _ = runs.create_or_get(
        AgentRunSpec(
            run_id="run:event-1",
            workflow_id="mattermost:root-1",
            actor_id="user-1",
            scope_id=scope,
            trace_id="trace-1",
            thread_id="mattermost:root-1",
            agent_ref="mmchat@1.0.0",
            package_snapshot={"package_hash": "parent"},
        )
    )
    parent = runs.transition(
        parent.run_id,
        AgentRunState.RUNNING,
        command_id="parent-running",
        expected_version=parent.version,
    )
    task = lifecycle.create(EntityType.TASK, "task:event-1", scope_id=scope)
    lifecycle.transition(
        EntityType.TASK,
        task.entity_id,
        "running",
        command_id="task-running",
        expected_version=task.version,
    )
    child, _ = runs.create_or_get(
        AgentRunSpec(
            run_id="delegate:project:child-1",
            workflow_id=parent.workflow_id,
            parent_run_id=parent.run_id,
            parent_tool_call_id="tool-1",
            actor_id="user-1",
            scope_id=scope,
            trace_id="trace-1",
            thread_id="delegate:project:child-1",
            agent_ref="project@1.0.0",
            package_snapshot={"package_hash": "child"},
        )
    )
    child = runs.transition(
        child.run_id,
        AgentRunState.RUNNING,
        command_id="child-running",
        expected_version=child.version,
    )
    runs.transition(
        child.run_id,
        AgentRunState.WAITING_APPROVAL,
        command_id="child-waiting",
        expected_version=child.version,
    )
    runs.transition(
        parent.run_id,
        AgentRunState.WAITING_CHILD,
        command_id="parent-waiting",
        expected_version=parent.version,
    )
    parent_snapshot = {
        "context": {
            "trace_id": "trace-1",
            "actor_id": "user-1",
            "conversation_id": "channel-1",
            "scope": scope,
            "run_id": "mattermost:root-1",
        }
    }
    child_resume = {
        "runtime": "deepagents",
        "thread_id": child.run_id,
        "tool_calls": [{"capability": "create_task", "tool_call_id": "action-0"}],
        "runtime_snapshot": {
            "context": {
                "trace_id": "trace-1",
                "actor_id": "user-1",
                "conversation_id": "channel-1",
                "scope": scope,
                "run_id": child.run_id,
            }
        },
        "governance_context": {"allowed_capabilities": ["create_task"]},
    }
    paused = AgentResult(
        "",
        "deepagents",
        status=RuntimeStatus.WAITING_APPROVAL,
        interruptions=(
            {
                "id": "parent-interrupt-1",
                "value": {
                    "runtime": "deepagents",
                    "thread_id": "mattermost:root-1",
                    "tool_calls": child_resume["tool_calls"],
                    "runtime_snapshot": parent_snapshot,
                    "capability_context": {
                        "trace_id": "trace-1",
                        "conversation_id": "channel-1",
                        "run_id": "mattermost:root-1",
                        "workflow_id": "mattermost:root-1",
                        "lifecycle_run_id": "run:event-1",
                    },
                    "delegated_child": {
                        "run_id": child.run_id,
                        "interrupt_id": "child-interrupt-1",
                        "resume": child_resume,
                    },
                },
            },
        ),
    )
    gateway = MagicMock(
        resume=AsyncMock(
            side_effect=[
                AgentResult("child done", "deepagents", output={"tasks": []}),
                AgentResult("parent done", "deepagents", output={"answer": "ok"}),
            ]
        )
    )
    coordinator = LangGraphApprovalCoordinator(
        store,
        lifecycle,
        ApprovalService(store, lifecycle),
        gateway,
        authorizer=StaticApprovalAuthorizer(frozenset({"user-1"})),
        access_guard=MagicMock(require=AsyncMock()),
        skill_registry=MagicMock(),
    )

    approval = coordinator.register(paused, requested_by="user-1", scope_id=scope)
    result = await coordinator.resume(
        approval.id,
        approved=True,
        actor_id="user-1",
        scope_id=scope,
        trace_id="trace-1",
    )

    assert result.output == {"answer": "ok"}
    assert [call.args[0] for call in gateway.resume.await_args_list] == [
        child.run_id,
        "mattermost:root-1",
    ]
    assert store.runs.get(child.run_id).state is AgentRunState.SUCCEEDED
    assert store.runs.get(child.run_id).result_envelope["result"] == {"tasks": []}
    assert store.runs.get(parent.run_id).state is AgentRunState.SUCCEEDED
    assert store.get_lifecycle_entity(EntityType.TASK, "task:event-1").state == "succeeded"
    store.close()
