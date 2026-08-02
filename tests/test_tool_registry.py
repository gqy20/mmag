import pytest

from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    bind_langgraph_capability,
)


class _AllowAuthorizer:
    def authorize(self, spec, arguments):
        del spec, arguments
        return CapabilityAuthorization.allow()


def _binding(spec: CapabilitySpec):
    return bind_langgraph_capability(spec, executor=CapabilityExecutor(_AllowAuthorizer()))


@pytest.mark.asyncio
async def test_execute_awaits_async_capability_results() -> None:
    async def collect_results():
        return [{"chunk": 1}, {"chunk": 2}]

    registry = CapabilityRegistry()
    registry.register(
        _binding(
            CapabilitySpec(
                name="collect_results",
                description="Collect results",
                input_schema={"type": "object", "properties": {}},
                handler=collect_results,
            )
        )
    )

    result = await registry.execute("collect_results", {})

    assert result.data == [{"chunk": 1}, {"chunk": 2}]


@pytest.mark.asyncio
async def test_execute_preserves_source_enrichment_for_json_text() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _binding(
            CapabilitySpec(
                name="external_search",
                description="search",
                input_schema={"type": "object", "properties": {}},
                handler=lambda: '{"results":[{"href":"https://example.com","title":"Example"}]}',
            )
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
