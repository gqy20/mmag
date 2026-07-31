"""离线消息主链契约测试。

覆盖 posted event → 持久化 → 显式路由 → Runtime → Mattermost 回复。
所有外部依赖均使用 mock，默认测试集合不得访问真实 Mattermost 或 LLM。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mmag.agent import Agent
from mmag.config import config
from mmag.sdk_llm import SDKLLMError


def _make_agent(runtime_result: str = "已完成") -> Agent:
    agent = Agent.__new__(Agent)
    agent.bot_user_id = "bot-1"
    agent.bot_username = "agent2"
    agent.stats = {"messages": 0, "responses": 0, "dropped_messages": 0}
    agent.working_memory = {}
    agent.tool_context = SimpleNamespace(current_post=None)

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
    agent.sdk_llm = MagicMock()
    agent.sdk_llm.agent_loop = AsyncMock(return_value=runtime_result)
    agent.llm = MagicMock()

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

    with patch.multiple(config, mm_channel_id="", mm_team_id="", use_sdk_llm=True):
        await agent._on_posted(_posted_event())

    assert agent.stats == {"messages": 1, "responses": 1, "dropped_messages": 0}
    agent.compactor.maybe_compact.assert_awaited_once_with("channel-1")
    agent.sdk_llm.agent_loop.assert_awaited_once()
    agent.mm.send_post.assert_called_once_with(
        channel_id="channel-1",
        message="任务完成",
        props={"from_bot": "true"},
    )


@pytest.mark.asyncio
async def test_explicit_message_delivers_user_visible_error_when_runtime_fails():
    agent = _make_agent()
    agent.sdk_llm.agent_loop.side_effect = SDKLLMError("model unavailable")

    with patch.multiple(config, mm_channel_id="", mm_team_id="", use_sdk_llm=True):
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

    with patch.multiple(config, mm_channel_id="", mm_team_id="", use_sdk_llm=True):
        await agent._on_posted(_posted_event())
        await agent._on_posted(_posted_event())

    assert agent.stats == {"messages": 1, "responses": 1, "dropped_messages": 0}
    assert agent.memory.has_message.call_count == 2
    persisted_ids = [call.args[0]["id"] for call in agent.memory.log_message.call_args_list]
    assert persisted_ids.count("post-1") == 1
    agent.sdk_llm.agent_loop.assert_awaited_once()
    agent.mm.send_post.assert_called_once()
