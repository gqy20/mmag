import json

import pytest

from mmag.tools import Tool, ToolRegistry


@pytest.mark.asyncio
async def test_execute_collects_async_generator_results() -> None:
    async def stream_results():
        yield {"chunk": 1}
        yield {"chunk": 2}

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="stream_results",
            description="Stream results",
            input_schema={"type": "object", "properties": {}},
            handler=stream_results,
        )
    )

    result = json.loads(await registry.execute("stream_results", {}))

    assert result == [{"chunk": 1}, {"chunk": 2}]


@pytest.mark.asyncio
async def test_execute_preserves_source_enrichment_for_json_text() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="external_search",
            description="search",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: '{"results":[{"href":"https://example.com","title":"Example"}]}',
        )
    )

    result = json.loads(await registry.execute("external_search", {}))

    assert result["_sources"] == [
        {
            "url": "https://example.com",
            "title": "Example",
            "tool": "external_search",
        }
    ]
