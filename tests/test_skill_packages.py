from pathlib import Path

import pytest

from mmag.agent_packages import AgentPackageError, AgentPackageRegistry, ContractAgentDecorator
from mmag.agent_system import AgentDescriptor, AgentRequest, RuntimeAgent, SkillInvocation
from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    bind_langgraph_capability,
)
from mmag.execution import ExecutionProfileRegistry
from mmag.governance import ModelPolicyRegistry, PolicyRegistry
from mmag.runtimes import AgentResult, RunContext, RunRequest
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


class _AllowAuthorizer:
    def authorize(self, spec, arguments):
        del spec, arguments
        return CapabilityAuthorization.allow()


def _capabilities() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    executor = CapabilityExecutor(_AllowAuthorizer())
    for name in (
        "analyze_link",
        "get_channel_info",
        "get_posts",
        "search_messages",
        "search_knowledge",
        "save_knowledge",
    ):
        registry.register(
            bind_langgraph_capability(
                CapabilitySpec(name, name, {"type": "object"}, lambda: {}),
                executor=executor,
            )
        )
    return registry


def _packages():
    skills = SkillPackageRegistry()
    skills.load_directory(ROOT / "skills")
    profiles = ExecutionProfileRegistry()
    profiles.load_directory(ROOT / "execution-profiles")
    policies = PolicyRegistry()
    policies.load_directory(ROOT / "policies")
    model_policies = ModelPolicyRegistry()
    model_policies.load_directory(ROOT / "model-policies")
    agents = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=model_policies,
        skill_registry=skills,
        execution_profile_registry=profiles,
    )
    agents.load_directory(ROOT / "agents")
    return skills, agents


def _skill(registry: SkillPackageRegistry, name: str):
    return next(item for item in registry.list() if item.manifest.metadata.name == name)


def test_loader_builds_versioned_skill_snapshot():
    package = SkillPackageLoader().load(ROOT / "skills" / "web-research")

    assert package.manifest.metadata.name == "web-research"
    assert package.snapshot.skill_version == package.manifest.metadata.version
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
    assert invocation.ref == _skill(skills, "web-research").manifest.metadata.ref
    assert invocation.capabilities == ("analyze_link", "search_knowledge")


def test_report_agent_resolves_bounded_meeting_summary_skill():
    skills, agents = _packages()
    package = agents.get("report")
    invocation = SkillResolver(skills, _capabilities()).resolve(
        package,
        AgentRequest(
            "meeting",
            "总结这个线程",
            requested_skill="meeting",
            parameters={
                "channel_id": "channel-1",
                "range": "thread",
                "root_post_id": "root-1",
                "anchor_post_id": "",
                "hours": 2,
                "limit": 100,
            },
        ),
        package.manifest.capabilities.allow,
    )

    assert invocation is not None
    assert invocation.ref == _skill(skills, "meeting").manifest.metadata.ref
    assert invocation.capabilities == ("get_posts",)


def test_skill_score_prefers_a_matching_personal_default():
    skills, _ = _packages()
    report = _skill(skills, "report")
    web = _skill(skills, "web-research")
    request = AgentRequest(
        "research",
        "调研市场并生成报告",
        preferred_skills=("web-research",),
    )

    assert SkillResolver._matches(report, request)
    assert SkillResolver._matches(web, request)
    assert SkillResolver._score(web, request) > SkillResolver._score(report, request)


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
    assert (
        f"version: {package.skills[invocation.ref].manifest.metadata.version}"
        in files["/skills/project/SKILL.md"]["content"]
    )


def test_skill_context_exposes_only_validated_selected_package():
    package = SkillPackageLoader().load(ROOT / "skills" / "project")
    context = SkillContext(package)

    with bind_skill_context(context):
        assert get_skill_context() is context
        assert get_skill_context().skill_ref == package.manifest.metadata.ref
    assert get_skill_context() is None


class StubRuntime:
    async def run(self, request):
        del request
        return AgentResult("done", "stub")


class MeetingRuntime:
    async def run(self, request):
        del request
        return AgentResult(
            "meeting summary",
            "stub",
            output={
                "title": "讨论纪要",
                "summary": "团队确认发布节奏。",
                "decisions": [
                    {"content": "周五发布", "source_post_ids": ["post-1"]}
                ],
                "action_items": [
                    {
                        "content": "整理发布说明",
                        "owner_username": "alice",
                        "due_text": None,
                        "due_at": None,
                        "source_post_ids": ["post-2"],
                    }
                ],
                "open_questions": [],
                "participants": ["alice"],
                "message_range": {
                    "type": "thread",
                    "message_count": 2,
                    "source_post_ids": ["post-1", "post-2"],
                },
                "coverage_notes": [],
            },
        )


@pytest.mark.asyncio
async def test_runtime_rejects_forged_skill_capability_expansion():
    _, agents = _packages()
    package = agents.get("mmchat")
    skill = next(
        item for item in package.skills.values()
        if item.manifest.metadata.name == "web-research"
    )
    forged = SkillInvocation(
        skill.manifest.metadata.ref,
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
            request_factory=lambda request, descriptor: RunRequest(
                RunContext("trace", "actor", "channel", request.scope),
                ({"role": "user", "content": request.prompt},),
            ),
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


@pytest.mark.asyncio
async def test_agent_envelope_accepts_the_selected_skills_valid_result_contract():
    skills, agents = _packages()
    package = agents.get("report")
    parameters = {
        "channel_id": "channel-1",
        "range": "thread",
        "root_post_id": "post-1",
        "anchor_post_id": "",
        "hours": 2,
        "limit": 100,
    }
    invocation = SkillResolver(skills, _capabilities()).resolve(
        package,
        AgentRequest(
            "meeting",
            "总结这个线程",
            requested_skill="meeting",
            parameters=parameters,
        ),
        package.manifest.capabilities.allow,
    )
    agent = ContractAgentDecorator(
        package,
        RuntimeAgent(
            AgentDescriptor(
                "report",
                "report",
                capabilities=package.manifest.capabilities.allow,
                scopes=("mattermost:*",),
            ),
            MeetingRuntime(),
            request_factory=lambda request, descriptor: request.runtime_request,
        ),
    )
    runtime_request = RunRequest(
        RunContext("trace", "actor", "channel-1", "mattermost:team/channel", run_id="run-1"),
        ({"role": "user", "content": "总结这个线程"},),
    )

    output = await agent.run(
        AgentRequest(
            "meeting",
            "总结这个线程",
            scope="mattermost:team/channel",
            actor_id="actor",
            task_id="task-1",
            run_id="run-1",
            parameters=parameters,
            runtime_request=runtime_request,
            skill=invocation,
        )
    )

    assert output.envelope["result"]["decisions"][0]["source_post_ids"] == ["post-1"]
    assert output.envelope["provenance"]["skill_name"] == "meeting"
