import asyncio

import pytest

from mmag.logger import trace


@pytest.mark.asyncio
async def test_trace_context_is_isolated_between_concurrent_tasks():
    ready = asyncio.Event()
    values = []

    async def observe(value: str):
        trace.new()
        trace.set_context(channel=value)
        if value == "b":
            ready.set()
        await ready.wait()
        await asyncio.sleep(0)
        values.append(trace.prefix())
        trace.clear()

    await asyncio.gather(observe("a"), observe("b"))
    assert any("channel=a" in value for value in values)
    assert any("channel=b" in value for value in values)
