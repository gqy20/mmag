import pytest

from mmag.capabilities import CapabilityBinding, CapabilityRegistry


@pytest.mark.asyncio
async def test_execute_collects_async_generator_results() -> None:
    async def stream_results():
        yield {"chunk": 1}
        yield {"chunk": 2}

    registry = CapabilityRegistry()
    registry.register(
        CapabilityBinding(
            name="stream_results",
            description="Stream results",
            input_schema={"type": "object", "properties": {}},
            handler=stream_results,
        )
    )

    result = await registry.execute("stream_results", {})

    assert result.data == [{"chunk": 1}, {"chunk": 2}]


@pytest.mark.asyncio
async def test_execute_preserves_source_enrichment_for_json_text() -> None:
    registry = CapabilityRegistry()
    registry.register(
        CapabilityBinding(
            name="external_search",
            description="search",
            input_schema={"type": "object", "properties": {}},
            handler=lambda: '{"results":[{"href":"https://example.com","title":"Example"}]}',
        )
    )

    result = await registry.execute("external_search", {})

    assert result.data["_sources"] == [
        {
            "url": "https://example.com",
            "title": "Example",
            "tool": "external_search",
        }
    ]
