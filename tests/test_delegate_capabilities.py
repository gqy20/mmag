import pytest

from mmag.agent_system import (
    AgentDescriptor,
    AgentDispatchResult,
    AgentDispatchTarget,
    AgentOutput,
    AgentRegistry,
    RunCoordinator,
    SkillInvocation,
)
from mmag.capabilities import CapabilityContext, CapabilitySuspension, bind_capability_context
from mmag.capabilities.delegate import create_delegate_capabilities
from mmag.control_plane import AgentRunService, AgentRunSpec, AgentRunState, SQLiteControlPlane
from mmag.runtimes import AgentResult, RuntimeStatus


class RecordingAgent:
    descriptor = AgentDescriptor("project", "project", capabilities=("create_task",))

    def __init__(self) -> None:
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return AgentOutput(
            "这段自由文本不应成为委托契约",
            "project",
            artifacts=(
                {"ref": "artifact://0123456789abcdef0123456789abcdef"},
                {"kind": "project_brief", "content": {}},
            ),
            result={"title": "项目计划", "tasks": []},
            envelope={"provenance": {"agent_ref": "project@1.2.0"}},
        )


class FailingAgent(RecordingAgent):
    async def run(self, request):
        self.requests.append(request)
        raise RuntimeError("child failed")


class WaitingAgent(RecordingAgent):
    async def run(self, request):
        self.requests.append(request)
        return AgentOutput(
            "waiting",
            "project",
            runtime_result=AgentResult(
                "waiting",
                "test",
                status=RuntimeStatus.WAITING_APPROVAL,
                interruptions=(
                    {
                        "id": "child-interrupt-1",
                        "value": {
                            "runtime": "deepagents",
                            "thread_id": request.run_id,
                            "native_request": {
                                "action_requests": [
                                    {"name": "create_task", "args": {"title": "任务"}}
                                ]
                            },
                            "runtime_snapshot": {"context": {"run_id": request.run_id}},
                        },
                    },
                ),
            ),
        )


class PackageSnapshot:
    agent_name = "project"
    agent_spec_version = "1.2.0"

    @staticmethod
    def to_dict():
        return {
            "agent_name": "project",
            "agent_spec_version": "1.2.0",
            "package_hash": "p1",
        }


class Package:
    snapshot = PackageSnapshot()


class PackageRegistry:

    def get(self, name):
        assert name == "project"
        return Package()


class SkillResolver:
    def __init__(self) -> None:
        self.requests = []

    def resolve(self, package, request, capabilities):
        self.requests.append((package, request, capabilities))
        return SkillInvocation(
            ref="project@1.2.1",
            capabilities=("create_task",),
            provenance={"skill_ref": "project@1.2.1"},
        )


class AuditSink:
    def __init__(self) -> None:
        self.events = []

    def append_audit(self, event_type, **kwargs):
        self.events.append((event_type, kwargs))
        return "audit-1"


def create_coordinator(agent, tmp_path):
    skills = SkillResolver()
    audit = AuditSink()
    store = SQLiteControlPlane(tmp_path / "delegation.db")
    runs = AgentRunService(store)
    parent, _ = runs.create_or_get(
        AgentRunSpec(
            run_id="run:parent-1",
            workflow_id="workflow-1",
            actor_id="user-1",
            scope_id="mattermost:install:team:chn:channel-1",
            trace_id="trace-1",
            thread_id="run:parent-1",
            agent_ref="mmchat@1.0.0",
            package_snapshot={"package_hash": "parent-hash"},
        )
    )
    runs.transition(
        parent.run_id,
        AgentRunState.RUNNING,
        command_id="start-parent",
        expected_version=parent.version,
    )
    coordinator = RunCoordinator(
        AgentRegistry((agent,)),
        PackageRegistry(),
        skills,
        runs,
        audit_sink=audit,
    )
    return coordinator, skills, audit, store


class TargetRecordingCoordinator:
    def __init__(self) -> None:
        self.targets = []

    async def dispatch(self, target, *, task, context, task_id):
        self.targets.append(target)
        return AgentDispatchResult(
            parent_run_id=context.run_id,
            run_id=f"delegate:{target.agent_name}:child",
            agent_name=target.agent_name,
            skill_ref=target.skill_name,
            status="succeeded",
            result={},
            artifact_refs=(),
            provenance={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_name", "agent_name", "intent", "skill_name"),
    (
        ("delegate_ppt", "ppt", "presentation", "slides"),
        ("delegate_report", "report", "report", "report"),
        ("delegate_project", "project", "project", "project"),
        ("delegate_link", "link", "link", ""),
    ),
)
async def test_delegate_uses_fixed_registered_target(
    capability_name, agent_name, intent, skill_name
):
    coordinator = TargetRecordingCoordinator()
    capability = next(
        item
        for item in create_delegate_capabilities(coordinator)
        if item.name == capability_name
    )
    context = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="delegate",
        scope="mattermost:install:team:chn:channel-1",
        run_id="mattermost:root-1",
        workflow_id="workflow-1",
        lifecycle_run_id="run:parent-1",
    )

    with bind_capability_context(context):
        await capability.handler(task="delegate")

    assert len(coordinator.targets) == 1
    assert coordinator.targets[0].agent_name == agent_name
    assert coordinator.targets[0].intent == intent
    assert coordinator.targets[0].skill_name == skill_name


@pytest.mark.asyncio
async def test_delegate_preserves_trusted_actor_and_scope(tmp_path):
    agent = RecordingAgent()
    coordinator, skills, audit, store = create_coordinator(agent, tmp_path)
    capability = next(
        item
        for item in create_delegate_capabilities(coordinator)
        if item.name == "delegate_project"
    )
    context = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="创建任务",
        scope="mattermost:install:team:chn:channel-1",
        run_id="run:parent-1",
        workflow_id="workflow-1",
        tool_call_id="tool-call-1",
    )

    with bind_capability_context(context):
        result = await capability.handler(task="创建任务")
        replay = await capability.handler(task="创建任务")

    assert result["status"] == "succeeded"
    assert len(agent.requests) == 1
    assert agent.requests[0].actor_id == "user-1"
    assert agent.requests[0].scope == "mattermost:install:team:chn:channel-1"
    assert agent.requests[0].task_id == "trace-1"
    assert agent.requests[0].requested_agent == "project"
    assert agent.requests[0].requested_skill == "project"
    assert agent.requests[0].skill.ref == "project@1.2.1"
    assert len(skills.requests) == 2
    assert result["child_run"]["parent_run_id"] == "run:parent-1"
    assert result["child_run"]["agent"] == "project"
    assert result["child_run"]["skill_ref"] == "project@1.2.1"
    assert result["result"] == {"title": "项目计划", "tasks": []}
    assert result["artifact_refs"] == [
        "artifact://0123456789abcdef0123456789abcdef"
    ]
    assert result["provenance"] == {"agent_ref": "project@1.2.0"}
    assert replay == result
    assert "text" not in result
    assert [event[1]["decision"] for event in audit.events] == [
        "running",
        "succeeded",
        "replayed",
    ]
    assert all(
        event[1]["details"]["parent_run_id"] == "run:parent-1" for event in audit.events
    )
    assert all(event[1]["details"]["workflow_id"] == "workflow-1" for event in audit.events)
    child = store.runs.get(result["child_run"]["run_id"])
    assert child.state is AgentRunState.SUCCEEDED
    assert child.result_envelope["result"] == {"title": "项目计划", "tasks": []}
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "expected_state"),
    ((FailingAgent(), AgentRunState.FAILED), (WaitingAgent(), AgentRunState.WAITING_APPROVAL)),
)
async def test_delegate_does_not_reexecute_failed_or_waiting_child(
    tmp_path, agent, expected_state
):
    coordinator, _, _, store = create_coordinator(agent, tmp_path)
    capability = next(
        item
        for item in create_delegate_capabilities(coordinator)
        if item.name == "delegate_project"
    )
    context = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="创建任务",
        scope="mattermost:install:team:chn:channel-1",
        run_id="run:parent-1",
        workflow_id="workflow-1",
        tool_call_id="terminal-tool-call",
    )

    with bind_capability_context(context):
        first = await capability.handler(task="创建任务")
        replay = await capability.handler(task="创建任务")

    assert len(agent.requests) == 1
    child = next(
        item
        for item in store.list_lifecycle_entities()
        if item.entity_id != "run:parent-1"
    )
    assert child.state == expected_state.value
    if expected_state is AgentRunState.FAILED:
        assert first["status"] == replay["status"] == "error"
    else:
        assert isinstance(first, CapabilitySuspension)
        assert isinstance(replay, CapabilitySuspension)
        assert first.value["child_run_id"] == replay.value["child_run_id"]
        assert store.runs.get("run:parent-1").state is AgentRunState.WAITING_CHILD
    store.close()


@pytest.mark.asyncio
async def test_delegate_claims_existing_queued_child_after_restart(tmp_path):
    agent = RecordingAgent()
    coordinator, _, _, store = create_coordinator(agent, tmp_path)
    context = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="创建任务",
        scope="mattermost:install:team:chn:channel-1",
        run_id="run:parent-1",
        workflow_id="workflow-1",
        tool_call_id="queued-tool-call",
    )
    store.runs.create_or_get(
        AgentRunSpec(
            run_id="queued-child",
            workflow_id="workflow-1",
            parent_run_id="run:parent-1",
            parent_tool_call_id="queued-tool-call",
            actor_id="user-1",
            scope_id="mattermost:install:team:chn:channel-1",
            trace_id="trace-1",
            thread_id="queued-child",
            agent_ref="project@1.2.0",
            skill_ref="project@1.2.1",
            package_snapshot={
                "agent_name": "project",
                "agent_spec_version": "1.2.0",
                "package_hash": "p1",
                "skill": {"skill_ref": "project@1.2.1"},
            },
        )
    )

    result = await coordinator.dispatch(
        AgentDispatchTarget("project", "project", "project"),
        task="创建任务",
        context=context,
        task_id="task-1",
    )

    assert result.run_id == "queued-child"
    assert result.status == "succeeded"
    assert len(agent.requests) == 1
    assert agent.requests[0].run_id == "queued-child"
    store.close()


@pytest.mark.asyncio
async def test_delegate_fails_closed_without_trusted_context(tmp_path):
    agent = RecordingAgent()
    coordinator, _, _, store = create_coordinator(agent, tmp_path)
    capability = next(
        item
        for item in create_delegate_capabilities(coordinator)
        if item.name == "delegate_project"
    )

    result = await capability.handler(task="创建任务")

    assert result["status"] == "error"
    assert agent.requests == []
    store.close()
