import asyncio
import json
from http.client import HTTPConnection
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from mmag.application.actions import (
    ActionCallbackServer,
    SlashCommandAdapter,
    SlashCommandService,
)
from mmag.control_plane import (
    EntityType,
    InboundEvent,
    LifecycleService,
    Scope,
    SQLiteControlPlane,
)


def command_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "token": "mattermost-command-token",
        "command": "/mmag",
        "text": "",
        "user_id": "user-1",
        "channel_id": "channel-1",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_empty_mmag_command_returns_ephemeral_subcommand_help():
    adapter = SlashCommandAdapter("mattermost-command-token")

    response = await adapter.handle(command_payload())

    assert response["response_type"] == "ephemeral"
    assert "/mmag help" in response["text"]
    assert "/mmag ask <goal>" in response["text"]
    assert "/mmag agents" in response["text"]


@pytest.mark.asyncio
async def test_unavailable_subcommand_returns_truthful_help():
    adapter = SlashCommandAdapter("mattermost-command-token")

    response = await adapter.handle(command_payload(text="ask write a report"))

    assert "`ask` 子命令尚未开放" in response["text"]
    assert "/mmag skills [agent]" in response["text"]


@pytest.mark.asyncio
async def test_mmag_command_rejects_invalid_token():
    adapter = SlashCommandAdapter("mattermost-command-token")

    with pytest.raises(PermissionError, match="invalid Mattermost slash command token"):
        await adapter.handle(command_payload(token="wrong-token"))


@pytest.mark.asyncio
async def test_callback_gateway_routes_form_to_slash_adapter():
    adapter = SlashCommandAdapter("mattermost-command-token")
    action_payloads: list[dict] = []

    async def handle_action(payload: dict) -> dict:
        action_payloads.append(payload)
        return {"update": {"message": "ok"}}

    server = ActionCallbackServer(
        "127.0.0.1",
        0,
        handle_action,
        command_callback=adapter.handle,
    )
    await server.start()
    assert server._server is not None
    port = server._server.server_port

    def post_command() -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = urlencode(command_payload())
            connection.request(
                "POST",
                "/integrations/commands",
                body,
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def post_action() -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = json.dumps({"user_id": "user-1", "context": {"token": "signed"}})
            connection.request(
                "POST",
                "/actions",
                body,
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, response = await asyncio.to_thread(post_command)
        action_status, action_response = await asyncio.to_thread(post_action)
    finally:
        await server.close()

    assert status == 200
    assert response["response_type"] == "ephemeral"
    assert "MMAG 子命令" in response["text"]
    assert action_status == 200
    assert action_response == {"update": {"message": "ok"}}
    assert action_payloads == [{"user_id": "user-1", "context": {"token": "signed"}}]


@pytest.mark.asyncio
async def test_callback_gateway_reports_real_readiness_state():
    ready = False
    server = ActionCallbackServer("127.0.0.1", 0, readiness=lambda: ready)
    await server.start()
    assert server._server is not None
    port = server._server.server_port

    def get(path: str) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        status, payload = await asyncio.to_thread(get, "/health/ready")
        ready = True
        ready_status, ready_payload = await asyncio.to_thread(get, "/health/ready")
        live_status, live_payload = await asyncio.to_thread(get, "/health/live")
    finally:
        await server.close()

    assert (status, payload) == (503, {"status": "not_ready"})
    assert (ready_status, ready_payload) == (200, {"status": "ready"})
    assert (live_status, live_payload) == (200, {"status": "live"})


class FakeRegistry:
    def __init__(self, *items):
        self.items = tuple(items)

    def list(self):
        return self.items

    def get(self, name: str):
        for item in self.items:
            if item.manifest.metadata.name == name:
                return item
        raise LookupError(name)


class FakeScopeResolver:
    def __init__(self):
        self.client = SimpleNamespace(
            get_post_async=self._get_post_async,
        )

    @staticmethod
    async def _get_post_async(post_id: str):
        return {"id": post_id, "channel_id": "channel-1", "root_id": ""}

    def resolve_post(self, post: dict) -> Scope:
        channel_id = str(post["channel_id"])
        return Scope(
            id=f"mattermost:install:tenant:chn:{channel_id}",
            conversation_id=channel_id,
        )


class FakeAccessGuard:
    def __init__(self, *, denied: bool = False):
        self.denied = denied
        self.calls: list[tuple[str, str, str]] = []

    async def require(self, actor_id: str, scope_id: str, *, channel_id: str = ""):
        self.calls.append((actor_id, scope_id, channel_id))
        if self.denied:
            raise PermissionError("membership denied")


class FakePipeline:
    def __init__(self):
        self.events = []

    async def accept(self, event):
        self.events.append(event)
        return True


def package_fixtures():
    skill_version = "8.7.6"
    skill_metadata = SimpleNamespace(
        name="web-research",
        version=skill_version,
        ref=f"web-research@{skill_version}",
        description="Research with source-aware evidence.",
    )
    skill = SimpleNamespace(manifest=SimpleNamespace(metadata=skill_metadata))
    agent_metadata = SimpleNamespace(
        name="mmchat",
        version="9.8.7",
        description="Mattermost conversation Agent.",
    )
    agent = SimpleNamespace(
        manifest=SimpleNamespace(
            metadata=agent_metadata,
            routing=SimpleNamespace(default=True),
        ),
        skills={skill_metadata.ref: skill},
    )
    return agent, skill


@pytest.mark.asyncio
async def test_agents_and_skills_are_read_from_activated_registries(tmp_path):
    agent, skill = package_fixtures()
    store = SQLiteControlPlane(str(tmp_path / "control.db"))
    guard = FakeAccessGuard()
    service = SlashCommandService(
        FakeRegistry(agent),
        FakeRegistry(skill),
        store,
        FakeScopeResolver(),
        guard,
    )
    adapter = SlashCommandAdapter("mattermost-command-token", service)

    try:
        agents = await adapter.handle(command_payload(text="agents"))
        skills = await adapter.handle(command_payload(text="skills"))
        agent_skills = await adapter.handle(command_payload(text="skills mmchat"))
    finally:
        store.close()

    assert f"`{agent.manifest.metadata.name}@{agent.manifest.metadata.version}` · 默认" in agents["text"]
    assert f"`{skill.manifest.metadata.ref}`" in skills["text"]
    assert "`mmchat` 可用 Skills" in agent_skills["text"]
    assert len(guard.calls) == 3


@pytest.mark.asyncio
async def test_summary_command_enters_the_existing_pipeline_with_trusted_scope(tmp_path):
    agent, skill = package_fixtures()
    store = SQLiteControlPlane(str(tmp_path / "control.db"))
    service = SlashCommandService(
        FakeRegistry(agent),
        FakeRegistry(skill),
        store,
        FakeScopeResolver(),
        FakeAccessGuard(),
    )
    pipeline = FakePipeline()
    service.attach_pipeline(pipeline)
    adapter = SlashCommandAdapter("mattermost-command-token", service)

    try:
        response = await adapter.handle(
            command_payload(text="summary today --tasks", trigger_id="trigger-1")
        )
    finally:
        store.close()

    assert "总结任务已排队" in response["text"]
    assert len(pipeline.events) == 1
    event = pipeline.events[0]
    post = event.payload["data"]["post"]
    assert event.event_id == "slash:trigger-1"
    assert event.event_type == "slash.summary"
    assert post["_mmag_entry"] == "slash"
    assert post["_mmag_summary"]["channel_id"] == "channel-1"
    assert post["_mmag_summary"]["tasks_only"] is True
    assert post["_mmag_summary"]["summary_period"] == "today"


@pytest.mark.asyncio
async def test_summary_thread_requires_an_explicit_root_post_id(tmp_path):
    agent, skill = package_fixtures()
    store = SQLiteControlPlane(str(tmp_path / "control.db"))
    service = SlashCommandService(
        FakeRegistry(agent),
        FakeRegistry(skill),
        store,
        FakeScopeResolver(),
        FakeAccessGuard(),
    )
    pipeline = FakePipeline()
    service.attach_pipeline(pipeline)
    adapter = SlashCommandAdapter("mattermost-command-token", service)

    try:
        missing = await adapter.handle(command_payload(text="summary thread"))
        accepted = await adapter.handle(
            command_payload(text="summary thread --root abc123 --tasks")
        )
    finally:
        store.close()

    assert "不提供当前 Thread ID" in missing["text"]
    assert "指定 Thread" in accepted["text"]
    assert len(pipeline.events) == 1
    post = pipeline.events[0].payload["data"]["post"]
    assert post["root_id"] == "abc123"
    assert post["_mmag_summary"]["range"] == "thread"


@pytest.mark.asyncio
async def test_status_only_returns_runs_owned_by_actor_in_current_channel(tmp_path):
    agent, skill = package_fixtures()
    store = SQLiteControlPlane(str(tmp_path / "control.db"))
    lifecycle = LifecycleService(store)
    for event_id, actor_id, channel_id in (
        ("owned", "user-1", "channel-1"),
        ("other-actor", "user-2", "channel-1"),
        ("other-channel", "user-1", "channel-2"),
    ):
        store.accept_event(
            InboundEvent(
                event_id=event_id,
                platform="mattermost",
                event_type="posted",
                conversation_id=channel_id,
                actor_id=actor_id,
                occurred_at=1.0,
                payload={},
            )
        )
        lifecycle.create(
            EntityType.AGENT_RUN,
            f"run:{event_id}",
            scope_id=channel_id,
            payload={"inbox_event_id": event_id},
        )
    guard = FakeAccessGuard()
    service = SlashCommandService(
        FakeRegistry(agent),
        FakeRegistry(skill),
        store,
        FakeScopeResolver(),
        guard,
    )
    adapter = SlashCommandAdapter("mattermost-command-token", service)

    try:
        recent = await adapter.handle(command_payload(text="status"))
        forbidden = await adapter.handle(command_payload(text="status other-actor"))
    finally:
        store.close()

    assert "`run:owned`" in recent["text"]
    assert "other-actor" not in recent["text"]
    assert "other-channel" not in recent["text"]
    assert "未找到当前频道中属于你的运行记录" in forbidden["text"]


@pytest.mark.asyncio
async def test_read_commands_fail_closed_when_membership_cannot_be_verified(tmp_path):
    agent, skill = package_fixtures()
    store = SQLiteControlPlane(str(tmp_path / "control.db"))
    service = SlashCommandService(
        FakeRegistry(agent),
        FakeRegistry(skill),
        store,
        FakeScopeResolver(),
        FakeAccessGuard(denied=True),
    )
    adapter = SlashCommandAdapter("mattermost-command-token", service)

    try:
        with pytest.raises(PermissionError, match="membership denied"):
            await adapter.handle(command_payload(text="agents"))
    finally:
        store.close()
