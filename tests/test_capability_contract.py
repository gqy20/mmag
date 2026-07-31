import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityStatus,
    SourcePolicy,
    bind_langgraph_capability,
    bind_sdk_capability,
    create_analyze_link_capability,
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_get_user_profile_capability,
    create_save_knowledge_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
)
from mmag.sdk_tools import create_sdk_tools
from mmag.tools import build_builtin_tools


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


def test_builtin_capability_visibility_is_identical_for_both_runtimes():
    client, memory = _client(), MagicMock()

    langgraph_names = [tool.name for tool in build_builtin_tools(client, memory)]
    sdk_names = [tool.name for tool in create_sdk_tools(client, memory)]

    assert langgraph_names == sdk_names
    assert langgraph_names == [
        "get_posts",
        "search_messages",
        "search_knowledge",
        "get_channel_info",
        "save_knowledge",
        "get_user_profile",
        "analyze_link",
        "send_file",
    ]


@pytest.mark.asyncio
async def test_langgraph_and_sdk_bindings_share_schema_handler_and_result():
    spec = create_get_channel_info_capability(_client())
    langgraph = bind_langgraph_capability(spec)
    sdk = bind_sdk_capability(spec)

    langgraph_result = await langgraph.handler(channel_id="channel-1")
    sdk_result = json.loads((await sdk.handler({"channel_id": "channel-1"}))["content"][0]["text"])

    assert langgraph.name == sdk.name == spec.name
    assert langgraph.description == sdk.description == spec.description
    assert langgraph.input_schema == spec.input_schema
    assert sdk.input_schema == dict(spec.input_schema)
    assert langgraph_result == sdk_result
    assert langgraph_result["display_name"] == "General"
    assert langgraph_result["type_label"] == "公开"


@pytest.mark.asyncio
async def test_capability_executor_returns_stable_invalid_input_error():
    spec = create_get_channel_info_capability(_client())
    langgraph = bind_langgraph_capability(spec)

    result = await langgraph.handler()

    assert result["error"]["code"] == CapabilityStatus.INVALID_INPUT
    assert "channel_id" in result["error"]["message"]


@pytest.mark.asyncio
async def test_search_knowledge_uses_one_default_and_limit_policy_for_both_bindings():
    memory = MagicMock()
    memory.get_relevant_knowledge.return_value = [
        {"key": "deploy", "value": "Use make deploy", "confidence": 0.9}
    ]
    spec = create_search_knowledge_capability(memory)
    langgraph = bind_langgraph_capability(spec)
    sdk = bind_sdk_capability(spec)

    langgraph_result = await langgraph.handler(channel_id="channel-1", query="deploy")
    sdk_result = json.loads(
        (await sdk.handler({"channel_id": "channel-1", "query": "deploy", "limit": 99}))["content"][
            0
        ]["text"]
    )

    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "memory:knowledge:read"
    assert spec.input_schema["properties"]["limit"]["default"] == 5
    assert spec.input_schema["required"] == ("channel_id", "query")
    assert langgraph.input_schema == spec.input_schema
    assert sdk.input_schema == dict(spec.input_schema)
    assert (
        langgraph_result
        == sdk_result
        == {
            "count": 1,
            "items": [{"key": "deploy", "value": "Use make deploy", "confidence": 0.9}],
        }
    )
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

    langgraph_result = await bind_langgraph_capability(spec).handler(channel_id="channel-1")
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler({"channel_id": "channel-1"}))["content"][0]["text"]
    )

    assert spec.permission == "mattermost:post:read"
    assert spec.input_schema["properties"]["limit"]["default"] == 30
    assert spec.input_schema["properties"]["limit"]["maximum"] == 100
    assert langgraph_result == sdk_result
    assert langgraph_result["count"] == 18
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
    result = await bind_langgraph_capability(spec).handler(channel_id="channel-1", limit=999)

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

    langgraph_result = await bind_langgraph_capability(spec).handler(**arguments)
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler(arguments))["content"][0]["text"]
    )

    assert spec.permission == "memory:messages:read"
    assert spec.input_schema["required"] == ()
    assert spec.input_schema["properties"]["limit"]["maximum"] == 50
    assert langgraph_result == sdk_result
    assert langgraph_result["messages"][0]["time_ms"] == 1_700_000_001_500
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


@pytest.mark.asyncio
async def test_user_profile_bindings_share_combined_result_and_ranking():
    memory, client = MagicMock(), MagicMock()
    memory.get_user_profile_decoded.return_value = {
        "message_count": 12,
        "topics": [f"topic-{index}" for index in range(12)],
        "active_hours": {"09": 2, "14": 5, "20": 3, "08": 1},
        "style": "简洁",
        "first_seen": "2026-01-01",
        "last_interaction": "2026-07-31",
    }
    client.get_username.return_value = "alice"
    spec = create_get_user_profile_capability(client, memory)

    langgraph_result = await bind_langgraph_capability(spec).handler(user_id="user-1")
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler({"user_id": "user-1"}))["content"][0]["text"]
    )

    assert spec.permission == "memory:user_profile:read"
    assert langgraph_result == sdk_result
    assert langgraph_result["topics"] == [f"topic-{index}" for index in range(2, 12)]
    assert langgraph_result["active_hours"] == ["14(5次)", "20(3次)", "09(2次)"]


@pytest.mark.asyncio
async def test_user_profile_preserves_empty_profile_message():
    memory, client = MagicMock(), MagicMock()
    memory.get_user_profile_decoded.return_value = {}
    client.get_username.return_value = "alice"

    result = await bind_langgraph_capability(
        create_get_user_profile_capability(client, memory)
    ).handler(user_id="user-1")

    assert result == {"username": "alice", "note": "暂无画像信息，该用户尚未发言或画像未建立"}


@pytest.mark.asyncio
async def test_analyze_link_applies_source_policy_equally_to_both_bindings(monkeypatch):
    analyze_url = AsyncMock(
        return_value={
            "url": "https://example.com/docs",
            "kind": "webpage",
            "status": "ok",
            "title": "Example Docs",
            "summary": "Useful content",
            "metadata": {"og": {"site_name": "Example"}},
        }
    )
    monkeypatch.setattr("mmag.url_analyzer.analyze_url", analyze_url)
    spec = create_analyze_link_capability(MagicMock())

    langgraph_result = await bind_langgraph_capability(spec).handler(url="https://example.com/docs")
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler({"url": "https://example.com/docs"}))["content"][
            0
        ]["text"]
    )

    assert spec.source_policy is SourcePolicy.AUTO
    assert langgraph_result == sdk_result
    assert langgraph_result["_sources"] == [
        {
            "url": "https://example.com/docs",
            "title": "Example Docs",
            "tool": "analyze_link",
            "kind": "webpage",
        }
    ]


@pytest.mark.asyncio
async def test_save_knowledge_is_one_declared_write_capability_for_both_bindings():
    memory = MagicMock()
    spec = create_save_knowledge_capability(memory)
    arguments = {"channel_id": "channel-1", "key": "deploy", "value": "Use make deploy"}

    langgraph_result = await bind_langgraph_capability(spec).handler(**arguments)
    sdk_result = json.loads(
        (await bind_sdk_capability(spec).handler(arguments))["content"][0]["text"]
    )

    assert spec.effect is CapabilityEffect.WRITE
    assert spec.permission == "memory:knowledge:write"
    assert (
        langgraph_result
        == sdk_result
        == {
            "status": "ok",
            "key": "deploy",
            "message": "已记住: deploy",
        }
    )
    assert memory.add_knowledge.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "expected_status"),
    [
        (CapabilityAuthorization.deny("write disabled"), CapabilityStatus.FORBIDDEN),
        (
            CapabilityAuthorization.require_approval("approval required"),
            CapabilityStatus.APPROVAL_REQUIRED,
        ),
    ],
)
async def test_write_policy_stops_handler_before_side_effect(authorization, expected_status):
    memory = MagicMock()
    authorizer = MagicMock()
    authorizer.authorize.return_value = authorization
    executor = CapabilityExecutor(authorizer=authorizer)
    tool = bind_langgraph_capability(create_save_knowledge_capability(memory), executor=executor)

    result = await tool.handler(channel_id="channel-1", key="deploy", value="secret")

    assert result["error"]["code"] == expected_status
    memory.add_knowledge.assert_not_called()
