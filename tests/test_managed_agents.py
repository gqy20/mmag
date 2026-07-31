import pytest

from mmag.capabilities import CapabilityExecutor, CapabilitySpec
from mmag.managed_agents import (
    AgentRegistry,
    AgentRouter,
    AgentRouteRequest,
    AgentSpec,
    HandoffOrchestrator,
    LinkAgent,
    ManagedAgentResult,
)


class StubAgent:
    def __init__(self, spec: AgentSpec, text: str):
        self.spec = spec
        self.text = text

    async def run(self, request: AgentRouteRequest) -> ManagedAgentResult:
        return ManagedAgentResult(text=self.text, agent_name=self.spec.name)


def test_registry_and_router_apply_permission_scope_cost_and_health():
    registry = AgentRegistry()
    registry.register(
        StubAgent(
            AgentSpec(
                name="link",
                description="Analyze links",
                intents=("link", "url"),
                permissions=("web:read",),
                scopes=("project:*",),
                max_cost_usd=0.2,
            ),
            "ok",
        )
    )
    router = AgentRouter(registry)
    route = router.route(
        AgentRouteRequest(
            intent="link",
            prompt="https://example.com",
            scope="project:alpha",
            permissions=frozenset({"web:read"}),
            budget_usd=0.5,
        )
    )
    assert route.spec.name == "link"


@pytest.mark.asyncio
async def test_handoff_has_explicit_steps_and_isolates_agent_failure():
    registry = AgentRegistry()
    first = StubAgent(AgentSpec("first", "first", intents=("one",)), "artifact")
    second = StubAgent(AgentSpec("second", "second", intents=("two",)), "done")
    registry.register(first)
    registry.register(second)
    result = await HandoffOrchestrator(registry).run(
        AgentRouteRequest(intent="one", prompt="start"),
        ("first", "missing", "second"),
    )
    assert [step.status for step in result.steps] == ["completed", "failed", "completed"]
    assert result.text == "done"


@pytest.mark.asyncio
async def test_link_agent_returns_a_structured_artifact():
    spec = CapabilitySpec(
        name="analyze_link",
        description="analyze",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        handler=lambda url: {"url": url, "title": "Example"},
    )
    result = await LinkAgent(spec, CapabilityExecutor()).run(
        AgentRouteRequest(
            intent="link",
            prompt="see https://example.com",
            permissions=frozenset({"web:read"}),
        )
    )
    assert result.artifacts[0]["kind"] == "link_analysis"
    assert result.artifacts[0]["source"] == "https://example.com"
