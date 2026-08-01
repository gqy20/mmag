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
        "mattermost:team-1/channel-1",
    )


@pytest.mark.asyncio
async def test_mattermost_approval_authorizer_allows_requester_without_lookup():
    client = AsyncMock()
    authorizer = MattermostApprovalAuthorizer(client)

    assert await authorizer.can_decide(_request(), "requester-1")
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
