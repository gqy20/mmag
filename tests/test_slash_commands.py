import asyncio
import json
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest

from mmag.application.actions import ActionCallbackServer, SlashCommandAdapter


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
    assert "当前已开放命令发现" in response["text"]


@pytest.mark.asyncio
async def test_unavailable_subcommand_returns_truthful_help():
    adapter = SlashCommandAdapter("mattermost-command-token")

    response = await adapter.handle(command_payload(text="agents"))

    assert "`agents` 子命令尚未开放" in response["text"]
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
    assert action_payloads == [
        {"user_id": "user-1", "context": {"token": "signed"}}
    ]
