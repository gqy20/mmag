from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mmag.capabilities import (
    CapabilityContext,
    CapabilityEffect,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    get_capability_context,
)
from mmag.capabilities.bindings import bind_langgraph_capability
from mmag.control_plane import (
    ApprovalService,
    EntityType,
    LangGraphApprovalCoordinator,
    LifecycleService,
    SQLiteControlPlane,
)
from mmag.control_plane.approval_policy import StaticApprovalAuthorizer
from mmag.governance import (
    GovernanceContext,
    PolicyCapabilityAuthorizer,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    bind_governance_context,
    get_governance_context,
)
from mmag.llm import ParsedResponse
from mmag.runtimes import (
    AgentResult,
    LangGraphRuntimeAdapter,
    RunContext,
    RunRequest,
    RuntimeStatus,
)


def _request(run_id: str = "run-1") -> RunRequest:
    return RunRequest(
        context=RunContext(
            trace_id="trace-1",
            actor_id="user-1",
            conversation_id="channel-1",
            scope="mattermost:team-1/channel-1",
            run_id=run_id,
        ),
        messages=({"role": "user", "content": "publish it"},),
        capabilities=(
            {
                "name": "publish",
                "description": "Publish a value",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        ),
    )


def _runtime(
    calls: list[str],
    responses: list[ParsedResponse],
    *,
    checkpoint_path: Path | None = None,
) -> tuple[LangGraphRuntimeAdapter, AsyncMock]:
    async def publish(value: str) -> dict[str, str]:
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
                reason="publication needs a human",
            ),
        )
    )
    executor = CapabilityExecutor(PolicyCapabilityAuthorizer(policy))
    registry = CapabilityRegistry()
    registry.register(bind_langgraph_capability(spec, executor=executor))
    backend = AsyncMock()
    backend.complete = AsyncMock(side_effect=responses)
    backend.chat = AsyncMock()
    runtime = LangGraphRuntimeAdapter(
        backend,
        capability_registry=registry,
        checkpoint_path=str(checkpoint_path) if checkpoint_path else None,
    )
    return runtime, backend.complete


def _tool_turn(value: str = "original") -> ParsedResponse:
    return ParsedResponse(
        tool_calls=[{"id": "call-1", "name": "publish", "input": {"value": value}}]
    )


@pytest.mark.asyncio
async def test_native_interrupt_pauses_before_side_effect_and_approve_resumes():
    calls: list[str] = []
    runtime, complete = _runtime(
        calls,
        [_tool_turn(), ParsedResponse(texts=["published"])],
    )
    context = GovernanceContext(
        "user-1",
        "mattermost:team-1/channel-1",
        roles=frozenset({"member"}),
        policy_ref="publish@1.0.0",
        allowed_capabilities=("publish",),
    )

    with bind_governance_context(context):
        paused = await runtime.run(_request())

    assert paused.status is RuntimeStatus.WAITING_APPROVAL
    assert calls == []
    payload = paused.interruptions[0]["value"]
    assert payload["kind"] == "tool_approval"
    assert payload["thread_id"] == "run-1"
    assert payload["tool_calls"][0]["capability"] == "publish"
    assert payload["governance_context"] == {
        "policy_ref": "publish@1.0.0",
        "allowed_capabilities": ["publish"],
        "roles": ["member"],
    }

    with bind_governance_context(context):
        completed = await runtime.resume(
            "run-1",
            {"decisions": [{"tool_call_id": "call-1", "decision": "approve"}]},
        )

    assert completed.status is RuntimeStatus.COMPLETED
    assert completed.text == "published"
    assert calls == ["original"]
    assert complete.await_count == 2


@pytest.mark.asyncio
async def test_native_review_can_edit_arguments_or_reject():
    edited_calls: list[str] = []
    edited_runtime, _ = _runtime(
        edited_calls,
        [_tool_turn(), ParsedResponse(texts=["edited and published"])],
    )
    context = GovernanceContext("user-1", "mattermost:team-1/channel-1")
    with bind_governance_context(context):
        await edited_runtime.run(_request("edit-run"))
        edited = await edited_runtime.resume(
            "edit-run",
            {
                "decisions": [
                    {
                        "tool_call_id": "call-1",
                        "decision": "edit",
                        "arguments": {"value": "reviewed"},
                    }
                ]
            },
        )
    assert edited.text == "edited and published"
    assert edited_calls == ["reviewed"]

    rejected_calls: list[str] = []
    rejected_runtime, _ = _runtime(
        rejected_calls,
        [_tool_turn(), ParsedResponse(texts=["not published"])],
    )
    with bind_governance_context(context):
        await rejected_runtime.run(_request("reject-run"))
        rejected = await rejected_runtime.resume(
            "reject-run",
            {"decisions": [{"tool_call_id": "call-1", "decision": "reject"}]},
        )
    assert rejected.text == "not published"
    assert rejected_calls == []


@pytest.mark.asyncio
async def test_sqlite_checkpoint_resumes_after_runtime_restart(tmp_path: Path):
    checkpoint = tmp_path / "agent.db"
    calls: list[str] = []
    first, _ = _runtime(calls, [_tool_turn()], checkpoint_path=checkpoint)
    context = GovernanceContext("user-1", "mattermost:team-1/channel-1")
    with bind_governance_context(context):
        paused = await first.run(_request("durable-run"))
    assert paused.status is RuntimeStatus.WAITING_APPROVAL
    await first.close()

    second, complete = _runtime(
        calls,
        [ParsedResponse(texts=["resumed after restart"])],
        checkpoint_path=checkpoint,
    )
    with bind_governance_context(context):
        result = await second.resume(
            "durable-run",
            {"decisions": [{"tool_call_id": "call-1", "decision": "approve"}]},
        )
    await second.close()

    assert result.text == "resumed after restart"
    assert calls == ["original"]
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_coordinator_keeps_business_lifecycle_in_sync(tmp_path: Path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    for entity_type, entity_id in (
        (EntityType.AGENT_RUN, "run:post-1"),
        (EntityType.TASK, "task:post-1"),
    ):
        lifecycle.create(entity_type, entity_id)
        lifecycle.transition(
            entity_type,
            entity_id,
            "running",
            command_id=f"start:{entity_id}",
        )
    gateway = AsyncMock()
    gateway.resume = AsyncMock(return_value=AgentResult("done", "langgraph"))
    coordinator = LangGraphApprovalCoordinator(
        store,
        lifecycle,
        ApprovalService(store, lifecycle),
        gateway,
        authorizer=StaticApprovalAuthorizer(frozenset({"reviewer-1"})),
    )
    paused = AgentResult(
        "",
        "langgraph",
        RuntimeStatus.WAITING_APPROVAL,
        interruptions=(
            {
                "id": "interrupt-1",
                "value": {
                    "thread_id": "mattermost:post-1",
                    "tool_calls": [
                        {
                            "tool_call_id": "call-1",
                            "capability": "publish",
                            "arguments": {"value": "x"},
                        }
                    ],
                },
            },
        ),
    )

    approval = coordinator.register(paused, requested_by="user-1", scope_id="scope-1")
    assert store.get_lifecycle_entity(EntityType.AGENT_RUN, "run:post-1").state == (
        "waiting_approval"
    )

    result = await coordinator.resume(
        approval.id,
        approved=True,
        actor_id="reviewer-1",
        scope_id="scope-1",
        trace_id="trace-1",
    )

    assert result.text == "done"
    assert store.get_lifecycle_entity(EntityType.AGENT_RUN, "run:post-1").state == "succeeded"
    assert store.get_lifecycle_entity(EntityType.TASK, "task:post-1").state == "succeeded"
    gateway.resume.assert_awaited_once_with(
        "mattermost:post-1",
        {"decisions": [{"tool_call_id": "call-1", "decision": "approve"}]},
    )
    store.close()


@pytest.mark.asyncio
async def test_approval_coordinator_rejects_unqualified_actor_without_resuming(tmp_path: Path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    gateway = AsyncMock()
    coordinator = LangGraphApprovalCoordinator(
        store,
        lifecycle,
        ApprovalService(store, lifecycle),
        gateway,
        authorizer=StaticApprovalAuthorizer(frozenset({"reviewer-1"})),
    )
    request = ApprovalService(store, lifecycle).request(
        "publish",
        {"thread_id": "run-1", "interrupt_id": "token-1", "tool_calls": []},
        requested_by="user-1",
        scope_id="scope-1",
        resume_token="token-1",
    )

    with pytest.raises(PermissionError, match="not authorized"):
        await coordinator.resume(
            request.id,
            approved=True,
            actor_id="member-1",
            scope_id="scope-1",
            trace_id="trace-1",
        )

    assert store.get_approval_request(request.id).state.value == "pending"
    gateway.resume.assert_not_awaited()
    store.close()


@pytest.mark.asyncio
async def test_approval_resume_restores_original_capability_context(tmp_path: Path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    observed_contexts: list[CapabilityContext | None] = []
    observed_governance: list[GovernanceContext | None] = []

    async def resume(thread_id, decisions):
        observed_contexts.append(get_capability_context())
        observed_governance.append(get_governance_context())
        return AgentResult("done", "langgraph")

    gateway = AsyncMock()
    gateway.resume = AsyncMock(side_effect=resume)
    coordinator = LangGraphApprovalCoordinator(
        store,
        lifecycle,
        ApprovalService(store, lifecycle),
        gateway,
        authorizer=StaticApprovalAuthorizer(frozenset({"reviewer-1"})),
    )
    paused = AgentResult(
        "",
        "langgraph",
        RuntimeStatus.WAITING_APPROVAL,
        interruptions=(
            {
                "id": "interrupt-1",
                "value": {
                    "thread_id": "run-1",
                    "tool_calls": [],
                    "governance_context": {
                        "policy_ref": "mmchat@1.0.0",
                        "allowed_capabilities": ["send_file"],
                        "roles": ["member"],
                    },
                },
            },
        ),
    )
    original = CapabilityContext(
        "trace-original",
        "user-1",
        "channel-1",
        "post-1",
        "请导出并发文件",
        "mattermost:team-1/channel-1",
    )
    approval = coordinator.register(
        paused,
        requested_by="user-1",
        scope_id=original.scope,
        capability_context=original,
    )

    await coordinator.resume(
        approval.id,
        approved=True,
        actor_id="reviewer-1",
        scope_id=original.scope,
        trace_id="trace-review",
    )

    assert observed_contexts == [original]
    assert observed_governance[0] is not None
    assert observed_governance[0].policy_ref == "mmchat@1.0.0"
    assert observed_governance[0].allowed_capabilities == ("send_file",)
    assert observed_governance[0].roles == frozenset({"member"})
    store.close()
