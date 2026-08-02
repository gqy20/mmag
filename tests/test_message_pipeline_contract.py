"""Offline posted-event contract through Router, Runtime and delivery."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mmag.agent_packages import AgentPackageLoader
from mmag.agent_system import AgentDescriptor, AgentRegistry, AgentRouter, RuntimeAgent
from mmag.application import (
    AttachmentProcessor,
    BotIdentity,
    ContextBuilder,
    MattermostDelivery,
    MessageHandler,
)
from mmag.capabilities import CapabilityRegistry, get_capability_context
from mmag.config import config
from mmag.runtimes import AgentResult, RunRequest, RuntimeUnavailableError

_PACKAGE = AgentPackageLoader().load(
    __import__("pathlib").Path(__file__).resolve().parents[1] / "agents/mmchat"
)
_SYSTEM_PROMPT = _PACKAGE.prompts[_PACKAGE.manifest.prompt.system_ref]


def _make_handler(runtime_result: str = "已完成"):
    stats = {"messages": 0, "responses": 0, "dropped_messages": 0}
    working_memory: dict[str, list] = {}
    identity = BotIdentity("bot-1", "agent2")
    mm = MagicMock()
    mm.get_username.return_value = "alice"
    mm.get_channel.return_value = {
        "id": "channel-1",
        "name": "general",
        "display_name": "General",
        "type": "O",
        "team_id": "team-1",
    }
    mm.send_post.return_value = "reply-1"
    mm.send_post_async = AsyncMock(return_value="reply-1")
    mm.update_post_async = AsyncMock(return_value="reply-1")
    memory = MagicMock()
    memory.has_message.return_value = False
    memory.log_message.return_value = True
    memory.get_user_profile_decoded.return_value = None
    memory.get_user_profile.return_value = {}
    memory.get_recent_summary.return_value = None
    memory.get_relevant_knowledge.return_value = []
    compactor = MagicMock()
    compactor.maybe_compact = AsyncMock()
    runtime = MagicMock()
    runtime.run = AsyncMock(return_value=AgentResult(text=runtime_result, runtime="test"))
    registry = AgentRegistry()
    registry.register(
        RuntimeAgent(
            AgentDescriptor(
                "mmchat",
                "mmchat",
                intents=("mention", "chat"),
                scopes=("mattermost:*",),
                max_cost_usd=config.model_budget_usd,
                is_default=True,
            ),
            runtime,
            request_factory=lambda request, descriptor: request.runtime_request,
        )
    )
    delivery = MattermostDelivery(mm, memory, identity, stats)
    delivery.send_ack = AsyncMock(return_value="")
    delivery.typing_loop = AsyncMock()
    delivery.typing_indicator = AsyncMock()
    attachments = AttachmentProcessor(mm)
    attachments.build_blocks = AsyncMock(return_value=None)
    handler = MessageHandler(
        mm_client=mm,
        memory=memory,
        compactor=compactor,
        capability_registry=CapabilityRegistry(),
        agent_router=AgentRouter(registry),
        skill_resolver=MagicMock(),
        audit_store=MagicMock(),
        approval_coordinator=MagicMock(),
        working_memory=working_memory,
        identity=identity,
        attachment_processor=attachments,
        context_builder=ContextBuilder(mm, memory, working_memory, identity, _SYSTEM_PROMPT),
        delivery=delivery,
        stats=stats,
    )
    return handler, runtime


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
async def test_explicit_message_routes_and_delivers_reply():
    handler, runtime = _make_handler("任务完成")
    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await handler.process_posted_event(_posted_event())
    assert handler.stats == {"messages": 1, "responses": 1, "dropped_messages": 0}
    handler.compactor.maybe_compact.assert_awaited_once_with("channel-1")
    runtime.run.assert_awaited_once()
    handler.delivery.send_ack.assert_awaited_once()
    sent = handler.mm.send_post_async.await_args.kwargs
    assert sent["channel_id"] == "channel-1"
    assert sent["root_id"] == "post-1"
    assert "任务完成" in sent["message"]
    assert sent["props"]["mmag_kind"] == "result"


@pytest.mark.asyncio
async def test_pipeline_ack_is_sent_at_acceptance_and_reused_by_processor():
    handler, runtime = _make_handler("任务完成")
    accepted = None

    class ImmediatePipeline:
        async def accept(self, event, *, on_accepted=None):
            nonlocal accepted
            accepted = event
            if on_accepted is not None:
                await on_accepted(event)
            return True

    handler.pipeline = ImmediatePipeline()
    handler.delivery.send_ack.return_value = "status-1"
    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await handler.on_posted(_posted_event())

    handler.delivery.send_ack.assert_awaited_once()
    runtime.run.assert_not_awaited()
    assert accepted is not None

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        messages = await handler.process_inbound(accepted)

    handler.delivery.send_ack.assert_awaited_once()
    assert messages[0].update_post_id == "status-1"


@pytest.mark.asyncio
async def test_runtime_failure_delivers_user_visible_error():
    handler, runtime = _make_handler()
    runtime.run.side_effect = RuntimeUnavailableError("model unavailable", runtime="test")
    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await handler.process_posted_event(_posted_event())
    sent = handler.mm.send_post_async.await_args.kwargs
    assert sent["root_id"] == "post-1"
    assert "外部服务不可用" in sent["message"]
    assert "mattermost:post-1" in sent["message"]


@pytest.mark.asyncio
async def test_plain_message_keeps_ack_when_agent_decides_silent():
    handler, runtime = _make_handler("<SILENT>")
    event = _posted_event()
    event["data"]["post"]["message"] = "普通频道消息"

    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await handler.process_posted_event(event)

    handler.delivery.send_ack.assert_awaited_once()
    runtime.run.assert_awaited_once()
    handler.mm.send_post_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_passes_provider_neutral_request_and_capability_context():
    handler, runtime = _make_handler("任务完成")
    observed = None

    async def observe(request):
        nonlocal observed
        observed = get_capability_context()
        assert isinstance(request, RunRequest)
        assert request.context.scope == "mattermost:team-1/channel-1"
        return AgentResult(text="任务完成", runtime="test")

    runtime.run.side_effect = observe
    with patch.multiple(config, mm_channel_id="", mm_team_id=""):
        await handler.process_posted_event(_posted_event())
    assert observed is not None
    assert observed.actor_id == "user-1"
    assert observed.conversation_id == "channel-1"
    assert observed.message_id == "post-1"
    assert get_capability_context() is None
