from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    CapabilityStatus,
    SourcePolicy,
    bind_langgraph_capability,
    build_builtin_bindings,
    create_analyze_link_capability,
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_save_knowledge_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
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


def _executor(authorization=None):
    authorizer = MagicMock()
    authorizer.authorize.return_value = authorization or CapabilityAuthorization.allow()
    return CapabilityExecutor(authorizer)


async def _execute(spec, **arguments):
    registry = CapabilityRegistry()
    registry.register(bind_langgraph_capability(spec, executor=_executor()))
    return (await registry.execute(spec.name, arguments)).to_payload()


def test_capability_declares_immutable_policy_metadata_once():
    spec = create_get_channel_info_capability(_client())

    assert spec.effect is CapabilityEffect.READ
    assert spec.permission == "mattermost:channel:read"
    assert spec.input_schema["required"] == ("channel_id",)
    with pytest.raises(FrozenInstanceError):
        spec.name = "changed"


def test_builtin_catalog_has_one_runtime_projection():
    names = [
        tool.name
        for tool in build_builtin_bindings(_client(), MagicMock(), executor=_executor())
    ]
    assert names == [
        "get_posts",
        "search_messages",
        "search_knowledge",
        "get_channel_info",
        "save_knowledge",
        "get_user_profile",
        "analyze_link",
        "send_file",
        "tencent_meeting_auth_login",
        "tencent_meeting_auth_status",
        "tencent_meeting_list_meetings",
        "tencent_meeting_list_ended_meetings",
        "tencent_meeting_get_meeting",
        "tencent_meeting_create_meeting",
        "tencent_meeting_cancel_meeting",
        "tencent_meeting_list_records",
        "tencent_meeting_get_smart_minutes",
        "tmeet_transcript_get",
        "tmeet_transcript_search",
        "tmeet_record_address",
        "tmeet_participants",
        "create_task",
        "list_tasks",
        "update_task",
        "get_task_overview",
        "create_goal",
        "list_goals",
        "update_goal",
        "get_goal_overview",
    ]


def test_schema_projection_does_not_expand_wildcard_to_unrelated_capabilities():
    registry = CapabilityRegistry()
    for name in ("task_create", "task_list", "send_file"):
        registry.register(
            bind_langgraph_capability(
                CapabilitySpec(name, name, {"type": "object"}, lambda: {}),
                executor=_executor(),
            )
        )

    schemas = registry.get_schema_list(("task_*",))

    assert [schema["name"] for schema in schemas] == ["task_create", "task_list"]


@pytest.mark.asyncio
async def test_binding_validates_input_and_returns_stable_error():
    result = await _execute(create_get_channel_info_capability(_client()))

    assert result["error"]["code"] == CapabilityStatus.INVALID_INPUT
    assert "channel_id" in result["error"]["message"]


@pytest.mark.asyncio
async def test_search_knowledge_applies_schema_default_and_limit():
    memory = MagicMock()
    memory.get_relevant_knowledge.return_value = [{"key": "deploy", "value": "make deploy"}]
    spec = create_search_knowledge_capability(memory)

    result = await _execute(
        spec,
        channel_id="channel-1",
        query="deploy",
        limit=99,
    )

    assert result["count"] == 1
    assert result["sources"][0]["source_system"] == "mmag.team_knowledge"
    assert len(result["sources"][0]["content_sha256"]) == 64
    memory.get_relevant_knowledge.assert_called_once_with("channel-1", "deploy", 10)


@pytest.mark.asyncio
async def test_search_messages_returns_bounded_source_refs():
    memory = MagicMock()
    memory.search_messages.return_value = [
        {
            "id": "post-1",
            "channel_id": "channel-1",
            "username": "alice",
            "message": "已经确认发布窗口",
            "create_at": 123.0,
        }
    ]

    result = await _execute(
        create_search_messages_capability(memory),
        query="发布窗口",
        channel_id="channel-1",
    )

    assert result["sources"][0]["resource_id"] == "post-1"
    assert result["sources"][0]["visible_scope_id"] == "channel-1"
    assert result["sources"][0]["snippet"] == "已经确认发布窗口"


@pytest.mark.asyncio
async def test_get_posts_uses_memory_without_rest_side_effects():
    memory, client = MagicMock(), MagicMock()
    memory.get_recent_messages.return_value = [
        {"username": "alice", "message": f"hello-{index}", "create_at": index}
        for index in range(18)
    ]

    result = await _execute(
        create_get_posts_capability(client, memory), channel_id="channel-1"
    )

    assert result["count"] == 18
    client.get_posts.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_link_adds_structured_source_metadata(monkeypatch):
    analyze_url = AsyncMock(
        return_value={
            "url": "https://example.com/docs",
            "kind": "webpage",
            "status": "ok",
            "title": "Example Docs",
            "summary": "Useful content",
            "metadata": {},
        }
    )
    monkeypatch.setattr("mmag.url_analyzer.analyze_url", analyze_url)
    spec = create_analyze_link_capability(MagicMock())

    result = await _execute(spec, url="https://example.com/docs")

    assert spec.source_policy is SourcePolicy.AUTO
    assert result["_sources"][0]["url"] == "https://example.com/docs"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "status"),
    [
        (CapabilityAuthorization.deny("disabled"), CapabilityStatus.FORBIDDEN),
        (
            CapabilityAuthorization.require_approval("review"),
            CapabilityStatus.APPROVAL_REQUIRED,
        ),
    ],
)
async def test_write_policy_stops_side_effect(authorization, status):
    memory = MagicMock()
    authorizer = MagicMock()
    authorizer.authorize.return_value = authorization
    tool = bind_langgraph_capability(
        create_save_knowledge_capability(memory),
        executor=CapabilityExecutor(authorizer),
    )

    registry = CapabilityRegistry()
    registry.register(tool)
    result = (
        await registry.execute(
            tool.name, {"channel_id": "channel-1", "key": "k", "value": "v"}
        )
    ).to_payload()

    assert result["error"]["code"] == status
    memory.add_knowledge.assert_not_called()
