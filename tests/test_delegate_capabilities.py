import pytest

from mmag.agent_system import (
    AgentDescriptor,
    AgentDispatcher,
    AgentDispatchResult,
    AgentOutput,
    AgentRegistry,
    SkillInvocation,
)
from mmag.capabilities import CapabilityContext, bind_capability_context
from mmag.capabilities.delegate import create_delegate_capabilities


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


class PackageRegistry:
    def get(self, name):
        assert name == "project"
        return object()


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


def create_dispatcher(agent):
    skills = SkillResolver()
    audit = AuditSink()
    dispatcher = AgentDispatcher(
        AgentRegistry((agent,)),
        PackageRegistry(),
        skills,
        audit_sink=audit,
    )
    return dispatcher, skills, audit


class TargetRecordingDispatcher:
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
    dispatcher = TargetRecordingDispatcher()
    capability = next(
        item
        for item in create_delegate_capabilities(dispatcher)
        if item.name == capability_name
    )
    context = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="delegate",
        scope="mattermost:install:team:chn:channel-1",
        run_id="run:parent-1",
        workflow_id="workflow-1",
    )

    with bind_capability_context(context):
        await capability.handler(task="delegate")

    assert len(dispatcher.targets) == 1
    assert dispatcher.targets[0].agent_name == agent_name
    assert dispatcher.targets[0].intent == intent
    assert dispatcher.targets[0].skill_name == skill_name


@pytest.mark.asyncio
async def test_delegate_preserves_trusted_actor_and_scope():
    agent = RecordingAgent()
    dispatcher, skills, audit = create_dispatcher(agent)
    capability = next(
        item
        for item in create_delegate_capabilities(dispatcher)
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
    )

    with bind_capability_context(context):
        result = await capability.handler(task="创建任务")

    assert result["status"] == "succeeded"
    assert len(agent.requests) == 1
    assert agent.requests[0].actor_id == "user-1"
    assert agent.requests[0].scope == "mattermost:install:team:chn:channel-1"
    assert agent.requests[0].task_id == "trace-1"
    assert agent.requests[0].requested_agent == "project"
    assert agent.requests[0].requested_skill == "project"
    assert agent.requests[0].skill.ref == "project@1.2.1"
    assert len(skills.requests) == 1
    assert result["child_run"]["parent_run_id"] == "run:parent-1"
    assert result["child_run"]["agent"] == "project"
    assert result["child_run"]["skill_ref"] == "project@1.2.1"
    assert result["result"] == {"title": "项目计划", "tasks": []}
    assert result["artifact_refs"] == [
        "artifact://0123456789abcdef0123456789abcdef"
    ]
    assert result["provenance"] == {"agent_ref": "project@1.2.0"}
    assert "text" not in result
    assert [event[1]["decision"] for event in audit.events] == ["running", "succeeded"]
    assert all(
        event[1]["details"]["parent_run_id"] == "run:parent-1" for event in audit.events
    )
    assert all(event[1]["details"]["workflow_id"] == "workflow-1" for event in audit.events)


@pytest.mark.asyncio
async def test_delegate_fails_closed_without_trusted_context():
    agent = RecordingAgent()
    dispatcher, _, _ = create_dispatcher(agent)
    capability = next(
        item
        for item in create_delegate_capabilities(dispatcher)
        if item.name == "delegate_project"
    )

    result = await capability.handler(task="创建任务")

    assert result["status"] == "error"
    assert agent.requests == []
