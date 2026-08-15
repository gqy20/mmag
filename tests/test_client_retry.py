from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from mmag.client import MMClient


@pytest.mark.parametrize(
    "transient_error",
    [
        requests.ConnectionError("connection reset"),
        requests.Timeout("request timed out"),
        requests.HTTPError(response=MagicMock(status_code=503)),
    ],
)
def test_send_post_retries_transient_failures_with_stable_pending_id(transient_error):
    client = MMClient(base_url="https://mattermost.example", token="token")
    payloads: list[dict] = []

    def post_side_effect(_path, **kwargs):
        payloads.append(dict(kwargs["json"]))
        if len(payloads) == 1:
            raise transient_error
        return {"id": "post-1"}

    client._post = MagicMock(side_effect=post_side_effect)

    with patch("time.sleep") as sleep:
        result = client.send_post("channel-1", "hello")

    assert result == "post-1"
    assert len(payloads) == 2
    assert payloads[0]["pending_post_id"]
    assert payloads[1]["pending_post_id"] == payloads[0]["pending_post_id"]
    sleep.assert_called_once()


def test_send_post_does_not_retry_business_4xx():
    client = MMClient(base_url="https://mattermost.example", token="token")
    error = requests.HTTPError(response=MagicMock(status_code=400))
    client._post = MagicMock(side_effect=error)

    with patch("time.sleep") as sleep:
        result = client.send_post("channel-1", "invalid")

    assert result is None
    client._post.assert_called_once()
    sleep.assert_not_called()


def test_send_post_stops_after_bounded_attempts():
    client = MMClient(base_url="https://mattermost.example", token="token")
    client._post = MagicMock(side_effect=requests.ConnectionError("offline"))

    with patch("time.sleep") as sleep:
        result = client.send_post("channel-1", "hello")

    assert result is None
    assert client._post.call_count == 3
    assert sleep.call_count == 2


@pytest.mark.asyncio
async def test_send_post_async_uses_caller_idempotency_key():
    client = MMClient(base_url="https://mattermost.example", token="token")
    response = MagicMock()
    response.json.return_value = {"id": "post-1"}
    client._request_async = AsyncMock(return_value=response)

    result = await client.send_post_async("channel-1", "hello", pending_post_id="delivery-1")

    assert result == "post-1"
    payload = client._request_async.await_args.kwargs["json"]
    assert payload["pending_post_id"] == "delivery-1"


@pytest.mark.asyncio
async def test_list_channel_users_uses_authoritative_membership_and_batched_users():
    client = MMClient(base_url="https://mattermost.example", token="token")
    first_members = MagicMock()
    first_members.json.return_value = [{"user_id": "u1"}, {"user_id": "u2"}]
    users = MagicMock()
    users.json.return_value = [
        {"id": "u1", "username": "alice"},
        {"id": "u2", "username": "bob"},
    ]
    client._request_async = AsyncMock(side_effect=[first_members, users])

    result = await client.list_channel_users_async("channel-1")

    assert tuple(user["id"] for user in result) == ("u1", "u2")
    calls = client._request_async.await_args_list
    assert calls[0].args == ("GET", "/channels/channel-1/members")
    assert calls[0].kwargs["params"] == {"page": 0, "per_page": 200}
    assert calls[1].args == ("POST", "/users/ids")
    assert calls[1].kwargs["json"] == ["u1", "u2"]


@pytest.mark.asyncio
async def test_list_channel_users_rejects_users_outside_membership_response():
    client = MMClient(base_url="https://mattermost.example", token="token")
    members = MagicMock()
    members.json.return_value = [{"user_id": "u1"}]
    users = MagicMock()
    users.json.return_value = [{"id": "u2", "username": "mallory"}]
    client._request_async = AsyncMock(side_effect=[members, users])

    with pytest.raises(RuntimeError, match="inconsistent"):
        await client.list_channel_users_async("channel-1")


@pytest.mark.asyncio
async def test_upload_file_async_uses_channel_query_and_multipart_filename():
    client = MMClient(base_url="https://mattermost.example", token="token")
    response = MagicMock()
    response.json.return_value = {"file_infos": [{"id": "file-1"}]}
    client._request_async = AsyncMock(return_value=response)

    result = await client.upload_file_async(
        "channel-1", "preview.png", b"png", "image/png"
    )

    assert result == "file-1"
    request = client._request_async.await_args
    assert request.args == ("POST", "/files")
    assert request.kwargs["params"] == {"channel_id": "channel-1"}
    assert request.kwargs["files"] == {
        "files": ("preview.png", b"png", "image/png")
    }
    assert "data" not in request.kwargs


def test_upload_file_uses_channel_query_and_multipart_filename():
    client = MMClient(base_url="https://mattermost.example", token="token")
    response = MagicMock()
    response.json.return_value = {"file_infos": [{"id": "file-1"}]}
    client.session.post = MagicMock(return_value=response)

    result = client.upload_file("channel-1", "deck.pptx", b"pptx")

    assert result == "file-1"
    request = client.session.post.call_args
    assert request.kwargs["params"] == {"channel_id": "channel-1"}
    assert request.kwargs["files"]["files"][:2] == ("deck.pptx", b"pptx")
    assert "data" not in request.kwargs
