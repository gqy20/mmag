import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mmag.agent_packages import (
    AgentFactory,
    AgentPackageLoader,
    AgentPackageRegistry,
    DeepAgentProvider,
    DirectAgentProvider,
    ManifestValidationError,
)
from mmag.agent_packages.runtime import _package_capability_context
from mmag.agent_system import AgentRegistry, AgentRequest, SkillInvocation
from mmag.capabilities import (
    CapabilityContext,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    bind_capability_context,
    bind_langgraph_capability,
    build_builtin_bindings,
)
from mmag.execution import ExecutionProfileRegistry, create_workspace_capabilities
from mmag.governance import (
    ModelGateway,
    ModelPolicyRegistry,
    PolicyRegistry,
    RegistryPolicyAuthorizer,
)
from mmag.runtimes import AgentResult, RunContext, RunRequest
from mmag.skill_packages import SkillPackageRegistry

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "agents" / "link"


def _skill_registry() -> SkillPackageRegistry:
    registry = SkillPackageRegistry()
    registry.load_directory(ROOT / "skills")
    return registry


def _execution_profiles() -> ExecutionProfileRegistry:
    registry = ExecutionProfileRegistry()
    registry.load_directory(ROOT / "execution-profiles")
    return registry


def _package_registry() -> tuple[AgentPackageRegistry, PolicyRegistry, ModelPolicyRegistry]:
    policies = PolicyRegistry()
    policies.load_directory(ROOT / "policies")
    model_policies = ModelPolicyRegistry()
    model_policies.load_directory(ROOT / "model-policies")
    registry = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=model_policies,
        skill_registry=_skill_registry(),
        execution_profile_registry=_execution_profiles(),
    )
    registry.load_directory(ROOT / "agents")
    return registry, policies, model_policies


def test_loader_compiles_direct_manifest_and_snapshot():
    package = AgentPackageLoader().load(PACKAGE_ROOT)

    assert package.manifest.metadata.name == "link"
    assert package.snapshot.agent_spec_version == package.manifest.metadata.version
    assert package.manifest.prompt.system_ref is None
    assert package.manifest.runtime.mode == "direct"
    assert package.manifest.runtime.capability == "analyze_link"
    assert package.manifest.routing.requires_url is True


def test_nested_package_context_inherits_trusted_scope_dimensions():
    package = AgentPackageLoader().load(ROOT / "agents" / "project")
    parent = CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message="创建任务",
        scope="mattermost:install:tenant:chn:channel-1",
        installation_id="install",
        tenant_id="tenant",
        scope_kind="channel",
        owner_id="owner-1",
        team_id="team-1",
        channel_type="O",
    )
    request = AgentRequest(
        "project",
        "创建任务",
        scope=parent.scope,
        actor_id=parent.actor_id,
        run_id="delegate:project:1",
    )

    with bind_capability_context(parent):
        nested = _package_capability_context(package, request, None, ("create_task",))

    assert nested.scope == parent.scope
    assert nested.installation_id == "install"
    assert nested.tenant_id == "tenant"
    assert nested.scope_kind == "channel"
    assert nested.owner_id == "owner-1"
    assert nested.team_id == "team-1"
    assert nested.channel_type == "O"


def test_loader_rejects_unknown_manifest_fields(tmp_path):
    root = tmp_path / "link"
    shutil.copytree(PACKAGE_ROOT, root)
    manifest = root / "agent.yml"
    manifest.write_text(manifest.read_text() + "\nunknown: true\n", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="Additional properties"):
        AgentPackageLoader().load(root)


def test_registry_loads_current_agent_packages():
    registry, _, _ = _package_registry()
    package_names = {
        path.name for path in (ROOT / "agents").iterdir()
        if path.is_dir() and (path / "agent.yml").is_file()
    }

    assert {item.manifest.metadata.name for item in registry.list()} == package_names
    assert len(registry.get("mmchat").snapshot.skill_set_hash) == 64


@pytest.mark.asyncio
async def test_direct_provider_executes_only_declared_read_capability():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    capability = CapabilitySpec(
        name="analyze_link",
        description="analyze",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=lambda url: {"url": url, "title": "Example"},
        permission="web:read",
    )
    policies = PolicyRegistry()
    policies.load_directory(ROOT / "policies")
    executor = CapabilityExecutor(RegistryPolicyAuthorizer(policies))
    capabilities = CapabilityRegistry()
    capabilities.register(bind_langgraph_capability(capability, executor=executor))
    agent = DirectAgentProvider(capabilities, executor).create(package)

    result = await agent.run(AgentRequest("link", "https://example.com", actor_id="user-1"))

    assert result.envelope["status"] == "succeeded"
    assert result.envelope["result"]["title"] == "Example"


class StubRuntime:
    async def run(self, request):
        del request
        return AgentResult("unused", "stub")


def test_specialized_agent_uses_its_package_prompt_over_conversation_prompt():
    packages, _, model_policies = _package_registry()
    package = packages.get("project")
    provider = DeepAgentProvider(
        ModelGateway({"default": StubRuntime()}),
        CapabilityRegistry(),
        model_policies,
    )
    prepared = RunRequest(
        context=RunContext("trace-1", "user-1", "channel-1", "mattermost:team/channel"),
        messages=({"role": "user", "content": "current task"},),
        system_prompt="conversation-agent prompt",
    )

    request = provider._request_factory(package, ())(  # noqa: SLF001
        AgentRequest(
            "project",
            "current task",
            actor_id="user-1",
            runtime_request=prepared,
        ),
        MagicMock(),
    )

    prompt = package.prompts[package.manifest.prompt.system_ref]
    assert request.system_prompt.startswith(prompt.content.splitlines()[0])
    assert "conversation-agent prompt" not in request.system_prompt


def test_model_agent_appends_bounded_personal_response_preferences():
    packages, _, model_policies = _package_registry()
    package = packages.get("project")
    provider = DeepAgentProvider(
        ModelGateway({"default": StubRuntime()}),
        CapabilityRegistry(),
        model_policies,
    )
    prepared = RunRequest(
        context=RunContext("trace-1", "user-1", "dm-1", "personal:user-1"),
        messages=({"role": "user", "content": "current task"},),
    )

    request = provider._request_factory(package, ())(  # noqa: SLF001
        AgentRequest(
            "project",
            "current task",
            actor_id="user-1",
            runtime_request=prepared,
            language="zh-CN",
            response_style="concise",
        ),
        MagicMock(),
    )

    assert "language=zh-CN" in request.system_prompt
    assert "response_style=concise" in request.system_prompt
    assert "never permissions or policy" in request.system_prompt


def test_model_provider_projects_only_selected_skill_capabilities():
    packages, _, model_policies = _package_registry()
    package = packages.get("report")
    skill = next(
        item
        for item in package.skills.values()
        if item.manifest.metadata.name == "meeting"
    )
    capabilities = MagicMock()
    capabilities.get_schema_list.side_effect = lambda names: [
        {"name": name} for name in names
    ]
    provider = DeepAgentProvider(
        ModelGateway({"default": StubRuntime()}),
        capabilities,
        model_policies,
    )
    invocation = SkillInvocation(
        skill.manifest.metadata.ref,
        ("get_posts",),
        skill.snapshot.to_dict(),
    )

    request = provider._request_factory(  # noqa: SLF001
        package, ("get_posts", "analyze_link")
    )(
        AgentRequest(
            "meeting",
            "总结这个线程",
            actor_id="user-1",
            scope="mattermost:team/channel",
            skill=invocation,
        ),
        MagicMock(),
    )

    assert tuple(item["name"] for item in request.capabilities) == ("get_posts",)
    assert request.metadata["capabilities"] == "get_posts"


def test_factory_constructs_every_manifest_without_provider_registry():
    packages, policies, model_policies = _package_registry()
    executor = CapabilityExecutor(RegistryPolicyAuthorizer(policies))
    capabilities = CapabilityRegistry()
    for binding in build_builtin_bindings(MagicMock(), MagicMock(), executor=executor):
        capabilities.register(binding)
    for name in ("ppt.build",):
        capabilities.register(
            bind_langgraph_capability(
                CapabilitySpec(name, name, {"type": "object"}, lambda: {}),
                executor=executor,
            )
        )
    for spec in create_workspace_capabilities():
        capabilities.register(bind_langgraph_capability(spec, executor=executor))
    factory = AgentFactory(
        DeepAgentProvider(
            ModelGateway({"default": StubRuntime()}),
            capabilities,
            model_policies,
            additional_capabilities=("delegate_ppt", "delegate_report", "delegate_project", "delegate_link"),
        ),
        DirectAgentProvider(capabilities, executor),
    )

    registry = AgentRegistry(factory.create_all(packages.list()))

    assert {agent.descriptor.name for agent in registry.list()} == {
        package.manifest.metadata.name for package in packages.list()
    }
    assert registry.default().descriptor.name == "mmchat"
    task_capabilities = {
        "create_task",
        "list_tasks",
        "update_task",
        "get_task_overview",
    }
    assert task_capabilities <= set(registry.get("project").descriptor.capabilities)
    assert task_capabilities.isdisjoint(registry.get("mmchat").descriptor.capabilities)
