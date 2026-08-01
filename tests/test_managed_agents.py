import pytest

from mmag.agent_system import (
    AgentDescriptor,
    AgentOutput,
    AgentRegistry,
    AgentRequest,
    AgentRouter,
    CapabilityAgent,
    HandoffCoordinator,
    RuntimeAgent,
)
from mmag.capabilities import CapabilityExecutor, CapabilitySpec
from mmag.runtimes import AgentResult, RunContext, RunRequest


class StubAgent:
    def __init__(self, descriptor: AgentDescriptor, text: str):
        self.descriptor = descriptor
        self.text = text

    async def run(self, request: AgentRequest) -> AgentOutput:
        return AgentOutput(text=self.text, agent_name=self.descriptor.name)


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return AgentResult("ok", "stub")


def test_registry_and_router_apply_permission_scope_cost_and_health():
    registry = AgentRegistry()
    registry.register(
        StubAgent(
            AgentDescriptor(
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
    selection = router.route(
        AgentRequest(
            intent="link",
            prompt="https://example.com",
            scope="project:alpha",
            permissions=frozenset({"web:read"}),
            budget_usd=0.5,
        )
    )
    assert selection.agent.descriptor.name == "link"
    assert selection.intent == "link"


@pytest.mark.asyncio
async def test_nondefault_runtime_agent_uses_its_own_request_factory():
    runtime = RecordingRuntime()
    prepared = RunRequest(
        RunContext("trace", "user", "channel", "mattermost:team/channel"),
        ({"role": "user", "content": "default prompt"},),
    )
    own = RunRequest(
        prepared.context,
        ({"role": "user", "content": "package prompt"},),
    )
    agent = RuntimeAgent(
        AgentDescriptor("report", "report"),
        runtime,
        request_factory=lambda request, descriptor: own,
        use_prepared_request=False,
    )

    await agent.run(AgentRequest("report", "build", runtime_request=prepared))

    assert runtime.requests == [own]


def test_router_prefers_yaml_specialization_and_normalizes_intent():
    default = StubAgent(
        AgentDescriptor(
            "mmchat",
            "default",
            intents=("chat", "mention"),
            scopes=("mattermost:*",),
            is_default=True,
        ),
        "chat",
    )
    link = StubAgent(
        AgentDescriptor(
            "link",
            "link",
            intents=("link",),
            scopes=("mattermost:*",),
            routing_priority=100,
            routing_keywords=("链接", "link"),
            requires_url=True,
        ),
        "link",
    )
    selection = AgentRouter(AgentRegistry((default, link))).route(
        AgentRequest(
            intent="mention",
            prompt="分析这个链接 https://example.com",
            scope="mattermost:team/channel",
        )
    )

    assert selection.agent.descriptor.name == "link"
    assert selection.intent == "link"


def test_registry_rejects_multiple_default_agents():
    first = StubAgent(AgentDescriptor("first", "first", is_default=True), "first")
    second = StubAgent(AgentDescriptor("second", "second", is_default=True), "second")

    with pytest.raises(ValueError, match="only one default"):
        AgentRegistry((first, second))


def test_registry_rejects_identical_specialized_routes():
    descriptor = AgentDescriptor(
        "first",
        "first",
        intents=("report",),
        routing_keywords=("报告",),
        routing_priority=10,
    )
    first = StubAgent(descriptor, "first")
    second = StubAgent(
        AgentDescriptor(
            "second",
            "second",
            intents=descriptor.intents,
            routing_keywords=descriptor.routing_keywords,
            routing_priority=descriptor.routing_priority,
        ),
        "second",
    )

    with pytest.raises(ValueError, match="ambiguous routing"):
        AgentRegistry((first, second))


@pytest.mark.asyncio
async def test_handoff_has_explicit_steps_and_isolates_agent_failure():
    registry = AgentRegistry()
    first = StubAgent(AgentDescriptor("first", "first", intents=("one",)), "artifact")
    second = StubAgent(AgentDescriptor("second", "second", intents=("two",)), "done")
    registry.register(first)
    registry.register(second)
    result = await HandoffCoordinator(registry).run(
        AgentRequest(intent="one", prompt="start"),
        ("first", "missing", "second"),
    )
    assert [step.status for step in result.steps] == ["completed", "failed", "completed"]
    assert result.text == "done"


@pytest.mark.asyncio
async def test_capability_agent_returns_a_structured_artifact():
    spec = CapabilitySpec(
        name="analyze_link",
        description="analyze",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        handler=lambda url: {"url": url, "title": "Example"},
    )
    result = await CapabilityAgent(
        AgentDescriptor("link", "link", capabilities=("analyze_link",)),
        spec,
        CapabilityExecutor(),
        source_argument="url",
        artifact_kind="link_analysis",
    ).run(
        AgentRequest(
            intent="link",
            prompt="see https://example.com",
            permissions=frozenset({"web:read"}),
        )
    )
    assert result.artifacts[0]["kind"] == "link_analysis"
    assert result.artifacts[0]["source"] == "https://example.com"
