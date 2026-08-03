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
from mmag.runtimes import (
    DeepAgentRuntime,
    RunContext,
    RunRequest,
    RuntimeLimitError,
    RuntimeStatus,
)
from mmag.runtimes.harness import (
    build_run_limit_middleware,
    build_state_filesystem_permissions,
    build_tool_visibility_middleware,
)
from mmag.runtimes.outputs import repair_structured_output


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
    executor = CapabilityExecutor(PolicyCapabilityAuthorizer(policy, audit_sink=audit_sink))
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


def test_repairs_only_schema_declared_json_containers():
    schema = {
        "type": "object",
        "properties": {
            "bundle": {
                "oneOf": [
                    {"type": "null"},
                    {"$ref": "#/$defs/bundle"},
                ]
            },
            "source": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {"type": "object", "properties": {"title": {"type": "string"}}},
            },
        },
        "$defs": {
            "bundle": {
                "type": "object",
                "required": ["pptx_ref"],
                "properties": {"pptx_ref": {"type": "string"}},
            }
        },
    }

    repaired, count = repair_structured_output(
        {
            "bundle": '{"pptx_ref":"artifact://deck"}',
            "source": '{"must":"remain text"}',
            "sections": '[{"title":"Overview"}]',
        },
        schema,
    )

    assert repaired == {
        "bundle": {"pptx_ref": "artifact://deck"},
        "source": '{"must":"remain text"}',
        "sections": [{"title": "Overview"}],
    }
    assert count == 2


def test_leaves_invalid_container_text_for_contract_validation():
    repaired, count = repair_structured_output(
        {"bundle": "not-json"},
        {
            "type": "object",
            "properties": {"bundle": {"type": ["object", "null"]}},
        },
    )

    assert repaired == {"bundle": "not-json"}
    assert count == 0


@pytest.mark.asyncio
async def test_schema_run_does_not_promote_json_text_to_structured_output():
    runtime = _runtime(
        [],
        AIMessage(
            content='{"summary":"text only"}',
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        ),
    )
    request = _request(runtime, "schema-run")
    request = RunRequest(
        request.context,
        request.messages,
        capabilities=request.capabilities,
        response_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )

    result = await runtime.run(request)

    assert result.output is None
    assert result.text == '{"summary":"text only"}'
    assert result.usage.model_calls == 1
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_plain_text_fills_only_the_single_text_result_contract():
    runtime = _runtime(
        [],
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "answer"},
            ],
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        ),
    )
    request = _request(runtime, "text-schema-run")
    request = RunRequest(
        request.context,
        request.messages,
        capabilities=request.capabilities,
        response_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    result = await runtime.run(request)

    assert result.output == {"text": "answer"}
    assert result.text == "answer"


def test_native_middleware_enforces_package_call_limits():
    request = RunRequest(
        RunContext("trace-1", "user-1", "channel-1", "scope-1"),
        ({"role": "user", "content": "hello"},),
        max_rounds=3,
        max_tool_calls=7,
    )

    model_limit, tool_limit = build_run_limit_middleware(request)

    assert model_limit.run_limit == model_limit.thread_limit == 3
    assert tool_limit.run_limit == tool_limit.thread_limit == 7
    assert model_limit.exit_behavior == tool_limit.exit_behavior == "error"


def test_native_filesystem_is_fail_closed_outside_state_workspace():
    permissions = build_state_filesystem_permissions()

    assert [(rule.operations, rule.paths, rule.mode) for rule in permissions] == [
        (["read"], ["/skills/**"], "allow"),
        (["write"], ["/skills/**"], "deny"),
        (["read", "write"], ["/workspace/**"], "allow"),
        (["read", "write"], ["/**"], "deny"),
    ]


def test_native_execute_is_visible_only_for_governed_workspace_runs():
    base = RunRequest(
        RunContext("trace-1", "user-1", "channel-1", "scope-1"),
        ({"role": "user", "content": "hello"},),
    )
    governed = RunRequest(
        base.context,
        base.messages,
        metadata={"capabilities": "workspace.read,workspace.execute"},
    )

    assert len(build_tool_visibility_middleware(base)) == 1
    assert build_tool_visibility_middleware(governed) == ()


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
                "access_context": {"actor_id": "user-1", "scope": "scope-1"},
            },
        )

    assert paused.status is RuntimeStatus.WAITING_APPROVAL
    assert completed.text == "done"
    assert calls == ["approved"]


@pytest.mark.asyncio
async def test_native_model_limit_persists_across_approval_resume():
    calls: list[str] = []
    runtime = _runtime(calls, _tool_call(), AIMessage(content="must not run"))
    request = _request(runtime, "limited-run")
    request = RunRequest(
        request.context,
        request.messages,
        capabilities=request.capabilities,
        max_rounds=1,
    )

    with bind_governance_context(GovernanceContext("user-1", "scope-1")):
        paused = await runtime.run(request)
        with pytest.raises(RuntimeLimitError):
            await runtime.resume(
                "limited-run",
                {
                    "decisions": [{"type": "approve"}],
                    "runtime_snapshot": paused.interruptions[0]["value"]["runtime_snapshot"],
                    "access_context": {"actor_id": "user-1", "scope": "scope-1"},
                },
            )

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
            {
                "decisions": [{"type": "approve"}],
                "runtime_snapshot": snapshot,
                "access_context": {"actor_id": "user-1", "scope": "scope-1"},
            },
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
        metadata={"agent_ref": "example-agent@2.1.0", "skill_ref": "example-skill@1.3.0"},
    )

    with bind_governance_context(GovernanceContext("user-1", "scope-1")):
        paused = await runtime.run(request)
        await runtime.resume(
            "observed-run",
            {
                "decisions": [{"type": "approve"}],
                "runtime_snapshot": paused.interruptions[0]["value"]["runtime_snapshot"],
                "access_context": {"actor_id": "user-1", "scope": "scope-1"},
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


@pytest.mark.asyncio
async def test_deep_agent_rejects_cross_scope_resume():
    calls: list[str] = []
    runtime = _runtime(calls, _tool_call(), AIMessage(content="done"))
    with bind_governance_context(GovernanceContext("user-1", "scope-1")):
        paused = await runtime.run(_request(runtime, "isolated-run"))
        with pytest.raises(PermissionError, match="scope"):
            await runtime.resume(
                "isolated-run",
                {
                    "decisions": [{"type": "approve"}],
                    "runtime_snapshot": paused.interruptions[0]["value"]["runtime_snapshot"],
                    "access_context": {"actor_id": "user-1", "scope": "scope-2"},
                },
            )

    assert calls == []
