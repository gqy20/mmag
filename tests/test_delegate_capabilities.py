import pytest

from mmag.agent_system import AgentDescriptor, AgentOutput, AgentRegistry
from mmag.capabilities import CapabilityContext, bind_capability_context
from mmag.capabilities.delegate import create_delegate_capabilities


class RecordingAgent:
    descriptor = AgentDescriptor("project", "project")

    def __init__(self) -> None:
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return AgentOutput("完成", "project")


@pytest.mark.asyncio
async def test_delegate_preserves_trusted_actor_and_scope():
    agent = RecordingAgent()
    registry = AgentRegistry((agent,))
    capability = next(
        item for item in create_delegate_capabilities(registry) if item.name == "delegate_project"
    )
    context = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="创建任务",
        scope="mattermost:install:team:chn:channel-1",
    )

    with bind_capability_context(context):
        result = await capability.handler(task="创建任务")

    assert result["status"] == "ok"
    assert len(agent.requests) == 1
    assert agent.requests[0].actor_id == "user-1"
    assert agent.requests[0].scope == "mattermost:install:team:chn:channel-1"
    assert agent.requests[0].task_id == "trace-1"


@pytest.mark.asyncio
async def test_delegate_fails_closed_without_trusted_context():
    agent = RecordingAgent()
    registry = AgentRegistry((agent,))
    capability = next(
        item for item in create_delegate_capabilities(registry) if item.name == "delegate_project"
    )

    result = await capability.handler(task="创建任务")

    assert result["status"] == "error"
    assert agent.requests == []
