"""离线消息主链契约测试。

覆盖 posted event → 持久化 → 显式路由 → Runtime → Mattermost 回复。
所有外部依赖均使用 mock，默认测试集合不得访问真实 Mattermost 或 LLM。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mmag.agent import Agent
from mmag.capabilities import get_capability_context
from mmag.config import config
from mmag.runtimes import AgentResult, RunRequest, RuntimeUnavailableError


def _make_agent(runtime_result: str = "已完成") -> Agent:
    agent = Agent.__new__(Agent)
    agent.bot_user_id = "bot-1"
    agent.bot_username = "agent2"
    agent.stats = {"messages": 0, "responses": 0, "dropped_messages": 0}
    agent.working_memory = {}
    agent.mm = MagicMock()
    agent.mm.get_username.return_value = "alice"
    agent.mm.get_channel.return_value = {
        "id": "channel-1",
        "name": "general",
        "display_name": "General",
        "type": "O",
        "team_id": "team-1",
    }
    agent.mm.send_post.return_value = "reply-1"

    agent.memory = MagicMock()
    agent.memory.has_message.return_value = False
    agent.memory.log_message.return_value = True
    agent.memory.get_user_profile_decoded.return_value = None
    agent.memory.get_user_profile.return_value = {}
    agent.memory.get_recent_summary.return_value = None
    agent.memory.get_relevant_knowledge.return_value = []

    agent.compactor = MagicMock()
    agent.compactor.maybe_compact = AsyncMock()
    agent.tool_registry = MagicMock()
    agent.tool_registry.get_schema_list.return_value = []
    agent.runtime = MagicMock()
    agent.runtime.run = AsyncMock(return_value=AgentResult(text=runtime_result, runtime="test"))

    agent._build_attachment_blocks = AsyncMock(return_value=None)
    agent._send_get_ack = AsyncMock()
    agent._typing_loop = AsyncMock()
    agent.typing_indicator = AsyncMock()
    return agent


def _posted_event() -> dict:
    return {
        "event": "posted",
        "data": {
            "post": {
                "id": "post-1",
                "channel_id": "channel-1",
                "user_id": "user-1",
                "message": "@agent2 帮我处理",
                "create_at": 1_753_929_600_000,
                "type": "",
                "root_id": "",
            }
        },
    }


@pytest.mark.asyncio
async def test_explicit_message_runs_offline_pipeline_and_delivers_reply():
    agent = _make_agent("任务完成")

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await agent._on_posted(_posted_event())

    assert agent.stats == {"messages": 1, "responses": 1, "dropped_messages": 0}
    agent.compactor.maybe_compact.assert_awaited_once_with("channel-1")
    agent.runtime.run.assert_awaited_once()
    agent.mm.send_post.assert_called_once_with(
        channel_id="channel-1",
        message="任务完成",
        props={"from_bot": "true"},
    )


@pytest.mark.asyncio
async def test_explicit_message_delivers_user_visible_error_when_runtime_fails():
    agent = _make_agent()
    agent.runtime.run.side_effect = RuntimeUnavailableError("model unavailable", runtime="test")

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await agent._on_posted(_posted_event())

    agent.mm.send_post.assert_called_once_with(
        channel_id="channel-1",
        message="⚠️ LLM 服务暂时不可用，请稍后再试。",
        props={"from_bot": "true"},
    )


@pytest.mark.asyncio
async def test_duplicate_post_is_not_persisted_or_replied_twice():
    agent = _make_agent("任务完成")
    agent.memory.has_message.side_effect = [False, True]

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await agent._on_posted(_posted_event())
        await agent._on_posted(_posted_event())

    assert agent.stats == {"messages": 1, "responses": 1, "dropped_messages": 0}
    assert agent.memory.has_message.call_count == 2
    persisted_ids = [call.args[0]["id"] for call in agent.memory.log_message.call_args_list]
    assert persisted_ids.count("post-1") == 1
    agent.runtime.run.assert_awaited_once()
    agent.mm.send_post.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_message_builds_provider_neutral_run_request():
    agent = _make_agent("任务完成")

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await agent._on_posted(_posted_event())

    request = agent.runtime.run.await_args.args[0]
    assert isinstance(request, RunRequest)
    assert request.context.actor_id == "user-1"
    assert request.context.conversation_id == "channel-1"
    assert request.context.trace_id != "----"
    assert request.context.scope == "mattermost:team-1/channel-1"
    assert request.max_rounds == config.max_tool_rounds


@pytest.mark.asyncio
async def test_runtime_executes_with_originating_message_capability_context():
    agent = _make_agent("任务完成")
    observed = None

    async def observe_context(_request):
        nonlocal observed
        observed = get_capability_context()
        return AgentResult(text="任务完成", runtime="test")

    agent.runtime.run.side_effect = observe_context

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await agent._on_posted(_posted_event())

    assert observed is not None
    assert observed.actor_id == "user-1"
    assert observed.conversation_id == "channel-1"
    assert observed.message_id == "post-1"
    assert observed.message == "@agent2 帮我处理"
    assert get_capability_context() is None
