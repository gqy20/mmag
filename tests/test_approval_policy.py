from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.control_plane import (
    ApprovalRequest,
    LangGraphApprovalCoordinator,
    MattermostAccessGuard,
    MattermostApprovalAuthorizer,
)


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
