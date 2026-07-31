from unittest.mock import MagicMock, patch

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
