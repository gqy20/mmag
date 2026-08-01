from pathlib import Path

import pytest

from mmag.agent_packages import AgentPackageError, AgentPackageRegistry, ContractAgentDecorator
from mmag.agent_system import AgentDescriptor, AgentRequest, RuntimeAgent, SkillInvocation
from mmag.capabilities import CapabilityBinding, CapabilityRegistry
from mmag.execution import ExecutionProfileRegistry
from mmag.runtimes import AgentResult
from mmag.skill_packages import (
    SkillContext,
    SkillPackageLoader,
    SkillPackageRegistry,
    SkillResolver,
    bind_skill_context,
    get_skill_context,
    project_skill_files,
)

ROOT = Path(__file__).resolve().parents[1]


def _capabilities() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for name in (
        "analyze_link",
        "get_channel_info",
        "get_posts",
        "search_messages",
        "search_knowledge",
        "save_knowledge",
    ):
        registry.register(CapabilityBinding(name, name, {"type": "object"}, lambda: {}))
    return registry


def _packages():
    skills = SkillPackageRegistry()
    skills.load_directory(ROOT / "skills")
    profiles = ExecutionProfileRegistry()
    profiles.load_directory(ROOT / "execution-profiles")
    agents = AgentPackageRegistry(skill_registry=skills, execution_profile_registry=profiles)
    agents.load_directory(ROOT / "agents")
    return skills, agents


def test_loader_builds_versioned_skill_snapshot():
    package = SkillPackageLoader().load(ROOT / "skills" / "web-research")

    assert package.manifest.metadata.ref == "web-research@1.2.0"
    assert package.manifest.capabilities.required == ("analyze_link",)
    assert len(package.snapshot.package_hash) == 64


def test_resolver_projects_agent_and_skill_capability_intersection():
    skills, agents = _packages()
    package = agents.get("mmchat")
    invocation = SkillResolver(skills, _capabilities()).resolve(
        package,
        AgentRequest("mention", "请调研三家竞品"),
        ("analyze_link", "search_knowledge", "save_knowledge"),
    )

    assert invocation is not None
    assert invocation.ref == "web-research@1.2.0"
    assert invocation.capabilities == ("analyze_link", "search_knowledge")


def test_selected_skill_projects_to_deep_agents_filesystem():
    skills, agents = _packages()
    package = agents.get("project")
    invocation = SkillResolver(skills, _capabilities()).resolve(
        package,
        AgentRequest("project", "整理项目状态", requested_skill="project"),
        package.manifest.capabilities.allow,
    )
    request = AgentRequest("project", "整理项目状态", skill=invocation)

    files = project_skill_files(package, request)

    assert "/skills/project/SKILL.md" in files
    assert "/skills/project/brief.md" in files
    assert "version: 1.2.0" in files["/skills/project/SKILL.md"]["content"]


def test_skill_context_exposes_only_validated_selected_package():
    package = SkillPackageLoader().load(ROOT / "skills" / "project")
    context = SkillContext(package)

    with bind_skill_context(context):
        assert get_skill_context() is context
        assert get_skill_context().skill_ref == "project@1.2.0"
    assert get_skill_context() is None


class StubRuntime:
    async def run(self, request):
        del request
        return AgentResult("done", "stub")


@pytest.mark.asyncio
async def test_runtime_rejects_forged_skill_capability_expansion():
    _, agents = _packages()
    package = agents.get("mmchat")
    skill = package.skills["web-research@1.2.0"]
    forged = SkillInvocation(
        "web-research@1.2.0",
        ("analyze_link", "save_knowledge"),
        skill.snapshot.to_dict(),
    )
    agent = ContractAgentDecorator(
        package,
        RuntimeAgent(
            AgentDescriptor(
                "mmchat",
                "chat",
                capabilities=("analyze_link", "save_knowledge"),
                scopes=("mattermost:*",),
            ),
            StubRuntime(),
        ),
    )

    with pytest.raises(AgentPackageError, match="invalid capability projection"):
        await agent.run(
            AgentRequest(
                "mention",
                "请调研竞品",
                scope="mattermost:team/channel",
                skill=forged,
            )
        )
