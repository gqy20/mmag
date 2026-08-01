from pathlib import Path

import pytest

from mmag.agent_packages import AgentPackageError, AgentPackageRegistry, ContractAgentDecorator
from mmag.agent_system import AgentDescriptor, AgentRequest, RuntimeAgent, SkillInvocation
from mmag.capabilities import CapabilityBinding, CapabilityRegistry
from mmag.runtimes import AgentResult, RunContext, RunRequest
from mmag.skill_packages import (
    SkillPackageLoader,
    SkillPackageRegistry,
    SkillReferenceError,
    SkillResolver,
    SkillResourceLoader,
    bind_skill_resource_session,
    build_skill_resource_catalog,
    load_active_skill_resource,
)

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "web-research"


def _capabilities() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for name in (
        "analyze_link",
        "load_skill_resource",
        "search_knowledge",
        "send_file",
    ):
        registry.register(CapabilityBinding(name, name, {"type": "object"}, lambda: {}))
    return registry


def _packages():
    skills = SkillPackageRegistry()
    skills.load_directory(ROOT / "skills")
    agents = AgentPackageRegistry(skill_registry=skills)
    agents.load_directory(ROOT / "agents")
    return skills, agents


def test_loader_builds_versioned_skill_snapshot_without_executing_resources():
    package = SkillPackageLoader().load(SKILL_ROOT)

    assert package.manifest.metadata.ref == "web-research@1.0.0"
    assert package.manifest.capabilities.required == (
        "analyze_link",
        "load_skill_resource",
    )
    assert package.manifest.resources.scripts == ()
    assert package.manifest.resources.references == ("references/source-quality.md",)
    assert len(package.snapshot.package_hash) == 64
    assert package.resources["templates/report.md"].sha256


def test_resolver_selects_only_agent_bound_skill_and_projects_capabilities():
    skills, agents = _packages()
    package = agents.get("mmchat")
    resolver = SkillResolver(skills, _capabilities())

    invocation = resolver.resolve(
        package,
        AgentRequest("mention", "请调研三家竞品"),
        ("analyze_link", "load_skill_resource", "search_knowledge", "send_file"),
    )

    assert invocation is not None
    assert invocation.ref == "web-research@1.0.0"
    assert invocation.capabilities == (
        "analyze_link",
        "load_skill_resource",
        "search_knowledge",
    )
    assert "send_file" not in invocation.capabilities


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return AgentResult("research complete", "stub")


@pytest.mark.asyncio
async def test_runtime_filters_tools_and_records_skill_provenance():
    skills, agents = _packages()
    package = agents.get("mmchat")
    capabilities = _capabilities()
    invocation = SkillResolver(skills, capabilities).resolve(
        package,
        AgentRequest("mention", "请调研三家竞品"),
        ("analyze_link", "load_skill_resource", "search_knowledge", "send_file"),
    )
    runtime = RecordingRuntime()
    agent = ContractAgentDecorator(
        package,
        RuntimeAgent(
            AgentDescriptor(
                "mmchat",
                "chat",
                capabilities=(
                    "analyze_link",
                    "load_skill_resource",
                    "search_knowledge",
                    "send_file",
                ),
                scopes=("mattermost:*",),
            ),
            runtime,
        ),
    )
    prepared = RunRequest(
        RunContext("trace", "user", "channel", "mattermost:team/channel"),
        ({"role": "user", "content": "research"},),
        capabilities=tuple(capabilities.get_schema_list()),
    )

    output = await agent.run(
        AgentRequest(
            "mention",
            "请调研三家竞品",
            scope="mattermost:team/channel",
            runtime_request=prepared,
            skill=invocation,
        )
    )

    assert {item["name"] for item in runtime.requests[0].capabilities} == {
        "analyze_link",
        "load_skill_resource",
        "search_knowledge",
    }
    assert output.envelope["provenance"]["skill_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_runtime_rejects_forged_skill_capability_expansion():
    _, agents = _packages()
    package = agents.get("mmchat")
    skill = package.skills["web-research@1.0.0"]
    forged = SkillInvocation(
        "web-research@1.0.0",
        skill.root.joinpath("SKILL.md").read_text(encoding="utf-8"),
        build_skill_resource_catalog(skill),
        ("analyze_link", "send_file"),
        skill.snapshot.to_dict(),
    )
    agent = ContractAgentDecorator(
        package,
        RuntimeAgent(
            AgentDescriptor(
                "mmchat",
                "chat",
                capabilities=("analyze_link", "send_file"),
                scopes=("mattermost:*",),
            ),
            RecordingRuntime(),
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


def test_resource_loader_discloses_only_requested_non_script_resources():
    package = SkillPackageLoader().load(SKILL_ROOT)
    session = SkillResourceLoader().create_session(package)

    assert session.provenance()["skill_resource_count"] == "0"
    with bind_skill_resource_session(session):
        loaded = load_active_skill_resource("templates/report.md")

    assert loaded["kind"] == "template"
    assert "# Research Result" in loaded["content"]
    assert session.provenance()["skill_resource_count"] == "1"
    assert "references/source-quality.md" not in session.provenance()[
        "skill_resources_json"
    ]
    with pytest.raises(SkillReferenceError, match="not declared"):
        session.load("scripts/not-allowed.py")


def test_resource_session_restores_only_previously_disclosed_refs():
    package = SkillPackageLoader().load(SKILL_ROOT)
    loader = SkillResourceLoader()
    original = loader.create_session(package)
    original.load("references/source-quality.md")

    restored = loader.restore_session(package, original.to_state())

    assert restored.provenance() == original.provenance()
    assert "templates/report.md" not in restored.provenance()["skill_resources_json"]


class DisclosingRuntime:
    async def run(self, request):
        del request
        load_active_skill_resource("references/source-quality.md")
        return AgentResult("verified research", "stub")


@pytest.mark.asyncio
async def test_runtime_provenance_contains_only_actually_loaded_resource():
    skills, agents = _packages()
    package = agents.get("mmchat")
    capabilities = _capabilities()
    invocation = SkillResolver(skills, capabilities).resolve(
        package,
        AgentRequest("mention", "请调研三家竞品"),
        tuple(capabilities.names()),
    )
    agent = ContractAgentDecorator(
        package,
        RuntimeAgent(
            AgentDescriptor(
                "mmchat",
                "chat",
                capabilities=tuple(capabilities.names()),
                scopes=("mattermost:*",),
            ),
            DisclosingRuntime(),
        ),
    )

    output = await agent.run(
        AgentRequest(
            "mention",
            "请调研三家竞品",
            scope="mattermost:team/channel",
            runtime_request=RunRequest(
                RunContext("trace", "user", "channel", "mattermost:team/channel"),
                ({"role": "user", "content": "research"},),
                capabilities=tuple(capabilities.get_schema_list()),
            ),
            skill=invocation,
        )
    )

    provenance = output.envelope["provenance"]
    assert provenance["skill_resource_count"] == "1"
    assert "references/source-quality.md" in provenance["skill_resources_json"]
    assert "templates/report.md" not in provenance["skill_resources_json"]
