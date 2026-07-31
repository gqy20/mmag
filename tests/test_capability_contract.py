import json
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from mmag.capabilities import (
    CapabilityEffect,
    CapabilityStatus,
    SourcePolicy,
    bind_legacy_capability,
    bind_sdk_capability,
    create_get_channel_info_capability,
)


def _client():
    client = MagicMock()
    client.get_channel.return_value = {
        "id": "channel-1",
        "name": "general",
        "display_name": "General",
        "type": "O",
    }
    return client


def test_get_channel_info_declares_policy_metadata_once():
    spec = create_get_channel_info_capability(_client())

    assert spec.name == "get_channel_info"
    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "mattermost:channel:read"
    assert spec.timeout_seconds == 10
    assert spec.source_policy is SourcePolicy.NONE
    assert spec.input_schema["required"] == ("channel_id",)
    with pytest.raises(FrozenInstanceError):
        spec.name = "changed"


@pytest.mark.asyncio
async def test_legacy_and_sdk_bindings_share_schema_handler_and_result():
    spec = create_get_channel_info_capability(_client())
    legacy = bind_legacy_capability(spec)
    sdk = bind_sdk_capability(spec)

    legacy_result = await legacy.handler(channel_id="channel-1")
    sdk_result = json.loads(
        (await sdk.handler({"channel_id": "channel-1"}))["content"][0]["text"]
    )

    assert legacy.name == sdk.name == spec.name
    assert legacy.description == sdk.description == spec.description
    assert legacy.input_schema == spec.input_schema
    assert sdk.input_schema == {"channel_id": str}
    assert legacy_result == sdk_result
    assert legacy_result["display_name"] == "General"
    assert legacy_result["type_label"] == "公开"


@pytest.mark.asyncio
async def test_capability_executor_returns_stable_invalid_input_error():
    spec = create_get_channel_info_capability(_client())
    legacy = bind_legacy_capability(spec)

    result = await legacy.handler()

    assert result["error"]["code"] == CapabilityStatus.INVALID_INPUT
    assert "channel_id" in result["error"]["message"]
