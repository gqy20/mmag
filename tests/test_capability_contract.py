import json
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, call

import pytest

from mmag.capabilities import (
    CapabilityEffect,
    CapabilityStatus,
    SourcePolicy,
    bind_legacy_capability,
    bind_sdk_capability,
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_search_messages_capability,
    create_search_knowledge_capability,
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


@pytest.mark.asyncio
async def test_search_knowledge_uses_one_default_and_limit_policy_for_both_bindings():
    memory = MagicMock()
    memory.get_relevant_knowledge.return_value = [
        {"key": "deploy", "value": "Use make deploy", "confidence": 0.9}
    ]
    spec = create_search_knowledge_capability(memory)
    legacy = bind_legacy_capability(spec)
    sdk = bind_sdk_capability(spec)

    legacy_result = await legacy.handler(channel_id="channel-1", query="deploy")
    sdk_result = json.loads(
        (
            await sdk.handler(
                {"channel_id": "channel-1", "query": "deploy", "limit": 99}
            )
        )["content"][0]["text"]
    )

    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "memory:knowledge:read"
    assert spec.input_schema["properties"]["limit"]["default"] == 5
    assert spec.input_schema["required"] == ("channel_id", "query")
    assert legacy.input_schema == spec.input_schema
    assert sdk.input_schema == {"channel_id": str, "query": str, "limit": int}
    assert legacy_result == sdk_result == {
        "count": 1,
        "items": [
            {"key": "deploy", "value": "Use make deploy", "confidence": 0.9}
        ],
    }
    assert memory.get_relevant_knowledge.call_args_list == [
        call("channel-1", "deploy", 5),
        call("channel-1", "deploy", 10),
    ]


@pytest.mark.asyncio
async def test_get_posts_bindings_share_cache_hit_behavior():
    memory, client = MagicMock(), MagicMock()
    memory.get_recent_messages.return_value = [
        {"username": "alice", "message": f"message-{index}", "create_at": index}
        for index in range(18)
    ]
    spec = create_get_posts_capability(client, memory)

    legacy_result = await bind_legacy_capability(spec).handler(channel_id="channel-1")
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler({"channel_id": "channel-1"}))["content"][0][
            "text"
        ]
    )

    assert spec.permission == "mattermost:post:read"
    assert spec.input_schema["properties"]["limit"]["default"] == 30
    assert spec.input_schema["properties"]["limit"]["maximum"] == 100
    assert legacy_result == sdk_result
    assert legacy_result["count"] == 18
    client.get_posts.assert_not_called()


@pytest.mark.asyncio
async def test_get_posts_caps_rest_fallback_and_backfills_cache():
    memory, client = MagicMock(), MagicMock()
    memory.get_recent_messages.return_value = []
    client.get_posts.return_value = [
        {"id": "post-1", "user_id": "user-1", "message": "hello", "create_at": 1}
    ]
    client.get_username.return_value = "alice"
    memory.log_message.return_value = True

    spec = create_get_posts_capability(client, memory)
    result = await bind_legacy_capability(spec).handler(channel_id="channel-1", limit=999)

    client.get_posts.assert_called_once_with("channel-1", limit=100)
    memory.log_message.assert_called_once()
    assert result["messages"][0] == {"user": "alice", "message": "hello", "time": 1}


@pytest.mark.asyncio
async def test_search_messages_bindings_share_filters_time_units_and_limit():
    memory = MagicMock()
    memory.search_messages.return_value = [
        {
            "channel_id": "channel-1",
            "username": "alice",
            "message": "deployment",
            "create_at": 1_700_000_001.5,
            "_score": -0.8,
        }
    ]
    spec = create_search_messages_capability(memory)
    arguments = {
        "query": "deploy",
        "channel_id": "channel-1",
        "user_id": "user-1",
        "before_ts": 0,
        "after_ts": 1_700_000_000_000,
        "limit": 999,
    }

    legacy_result = await bind_legacy_capability(spec).handler(**arguments)
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler(arguments))["content"][0]["text"]
    )

    assert spec.permission == "memory:messages:read"
    assert spec.input_schema["required"] == ()
    assert spec.input_schema["properties"]["limit"]["maximum"] == 50
    assert legacy_result == sdk_result
    assert legacy_result["messages"][0]["time_ms"] == 1_700_000_001_500
    assert memory.search_messages.call_args_list == [
        call(
            query="deploy",
            channel_id="channel-1",
            user_id="user-1",
            before_ts=0.0,
            after_ts=1_700_000_000.0,
            limit=50,
        ),
        call(
            query="deploy",
            channel_id="channel-1",
            user_id="user-1",
            before_ts=0.0,
            after_ts=1_700_000_000.0,
            limit=50,
        ),
    ]
