from unittest.mock import AsyncMock

import pytest

from mmag.application import MattermostStream
from mmag.runtimes import RunEvent, RunEventKind


@pytest.mark.asyncio
async def test_mattermost_stream_creates_then_updates_one_post_per_round():
    mm = AsyncMock()
    mm.send_post_async.return_value = "stream-1"
    mm.update_post_async.return_value = "stream-1"
    stream = MattermostStream(
        mm,
        {"id": "post-1", "channel_id": "channel-1"},
        "run-1",
        min_interval_seconds=0,
        min_chars=1,
    )

    await stream(RunEvent(RunEventKind.TEXT_DELTA, "Hel", 1))
    await stream(RunEvent(RunEventKind.TEXT_DELTA, "lo", 1))
    await stream(RunEvent(RunEventKind.TEXT_DELTA, "Final", 2))

    mm.send_post_async.assert_awaited_once()
    assert mm.update_post_async.await_count == 2
    final_message = mm.update_post_async.await_args_list[-1].args[1]
    assert "Final" in final_message
    assert "Hello" not in final_message
    assert stream.post_id == "stream-1"
