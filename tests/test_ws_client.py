import asyncio
import json
from unittest.mock import AsyncMock, patch

from mmag.ws_client import WebSocketClient


async def test_websocket_becomes_ready_after_authenticated_hello():
    client = WebSocketClient("ws://localhost/api/v4/websocket", "secret")
    websocket = AsyncMock()
    websocket.recv.return_value = json.dumps(
        {
            "event": "hello",
            "seq": 0,
            "data": {"connection_id": "connection-1", "server_version": "11.8.0"},
        }
    )
    websocket.__aiter__.return_value = iter(())
    connection = AsyncMock()
    connection.__aenter__.return_value = websocket
    connection.__aexit__.return_value = False

    assert not client.is_ready
    with patch("mmag.ws_client.websockets.connect", return_value=connection):
        task = asyncio.create_task(client._session())
        await client.wait_until_ready()
        assert client.is_ready
        await task

    sent_payloads = [json.loads(call.args[0]) for call in websocket.send.await_args_list]
    assert all(payload.get("action") != "authentication_challenge" for payload in sent_payloads)
