import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from mmag.capabilities import (
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    bind_langgraph_capability,
)
from mmag.control_plane import SQLiteControlPlane
from mmag.governance import (
    GovernanceContext,
    PolicyCapabilityAuthorizer,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    bind_governance_context,
)
from mmag.runtimes import DeepAgentRuntime, RunContext, RunRequest, RuntimeStatus


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "stub"

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del messages, stop, run_manager, kwargs
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


class ModelFactory:
    def __init__(self, *responses: AIMessage):
        self.model = ScriptedModel(responses=list(responses))

    def create(self, **kwargs):
        del kwargs
        return self.model


def _runtime(
    calls: list[str],
    *responses: AIMessage,
    checkpointer=None,
    audit_sink=None,
) -> DeepAgentRuntime:
    async def publish(value: str):
        calls.append(value)
        return {"published": value}

    spec = CapabilitySpec(
        "publish",
        "Publish a value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        publish,
        effect=CapabilityEffect.WRITE,
        permission="content.publish",
    )
    policy = PolicyEngine(
        (
            PolicyRule(
                "review-publish",
                PolicyEffect.REQUIRE_APPROVAL,
                permissions=("content.publish",),
            ),
        )
    )
    executor = CapabilityExecutor(PolicyCapabilityAuthorizer(policy))
    registry = CapabilityRegistry()
    registry.register(bind_langgraph_capability(spec, executor=executor))
    return DeepAgentRuntime(
        registry,
        checkpointer=checkpointer,
        model_factory=ModelFactory(*responses),
        audit_sink=audit_sink,
    )


def _request(runtime: DeepAgentRuntime, run_id: str) -> RunRequest:
    return RunRequest(
        RunContext("trace-1", "user-1", "channel-1", "scope-1", run_id=run_id),
        ({"role": "user", "content": "publish"},),
        capabilities=tuple(runtime.capability_registry.get_schema_list()),
    )


def _tool_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "publish",
                "args": {"value": "approved"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.asyncio
async def test_deep_agent_interrupts_before_side_effect_and_resumes():
    calls: list[str] = []
    runtime = _runtime(calls, _tool_call(), AIMessage(content="done"))
    context = GovernanceContext("user-1", "scope-1")

    with bind_governance_context(context):
        paused = await runtime.run(_request(runtime, "run-1"))
        completed = await runtime.resume(
            "run-1",
            {
                "decisions": [{"type": "approve"}],
                "runtime_snapshot": paused.interruptions[0]["value"]["runtime_snapshot"],
            },
        )

    assert paused.status is RuntimeStatus.WAITING_APPROVAL
    assert completed.text == "done"
    assert calls == ["approved"]


@pytest.mark.asyncio
async def test_deep_agent_rebuilds_graph_from_approval_snapshot():
    checkpointer = InMemorySaver()
    calls: list[str] = []
    first = _runtime(calls, _tool_call(), checkpointer=checkpointer)
    context = GovernanceContext("user-1", "scope-1")
    with bind_governance_context(context):
        paused = await first.run(_request(first, "durable-run"))
    snapshot = paused.interruptions[0]["value"]["runtime_snapshot"]
    await first.close()

    second = _runtime(calls, AIMessage(content="resumed"), checkpointer=checkpointer)
    with bind_governance_context(context):
        completed = await second.resume(
            "durable-run",
            {"decisions": [{"type": "approve"}], "runtime_snapshot": snapshot},
        )
    await second.close()

    assert completed.text == "resumed"
    assert calls == ["approved"]


@pytest.mark.asyncio
async def test_native_callbacks_write_content_free_model_and_tool_audits(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "telemetry.db"))
    calls: list[str] = []
    runtime = _runtime(
        calls,
        _tool_call(),
        AIMessage(content="done"),
        audit_sink=store,
    )
    request = _request(runtime, "observed-run")
    request = RunRequest(
        request.context,
        request.messages,
        capabilities=request.capabilities,
        metadata={"agent_ref": "report@2.1.0", "skill_ref": "report@1.3.0"},
    )

    with bind_governance_context(GovernanceContext("user-1", "scope-1")):
        paused = await runtime.run(request)
        await runtime.resume(
            "observed-run",
            {
                "decisions": [{"type": "approve"}],
                "runtime_snapshot": paused.interruptions[0]["value"]["runtime_snapshot"],
            },
        )

    model_audits = store.list_audits(event_type="model.call")
    tool_audits = store.list_audits(event_type="capability.call", target="publish")
    policy_audits = store.list_audits(event_type="policy.decision", target="publish")
    assert {item.decision for item in model_audits} >= {"running", "succeeded"}
    assert {item.decision for item in tool_audits} == {"running", "succeeded"}
    assert {item.decision for item in policy_audits} == {"require_approval"}
    assert all("publish" not in str(item.details) for item in model_audits)
    assert all("approved" not in str(item.details) for item in tool_audits)
