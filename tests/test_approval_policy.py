from unittest.mock import AsyncMock

import pytest

from mmag.control_plane import ApprovalRequest, MattermostApprovalAuthorizer


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
