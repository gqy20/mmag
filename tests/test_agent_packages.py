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
from mmag.agent_system import AgentRegistry, AgentRequest
from mmag.capabilities import (
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
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


def test_loader_compiles_direct_manifest_and_version():
    package = AgentPackageLoader().load(PACKAGE_ROOT)

    assert package.manifest.metadata.name == "link"
    assert package.snapshot.agent_spec_version == "2.0.0"
    assert package.manifest.prompt.system_ref is None
    assert package.manifest.runtime.mode == "direct"
    assert package.manifest.runtime.capability == "analyze_link"
    assert package.manifest.routing.requires_url is True


def test_loader_rejects_unknown_manifest_fields(tmp_path):
    root = tmp_path / "link"
    shutil.copytree(PACKAGE_ROOT, root)
    manifest = root / "agent.yml"
    manifest.write_text(manifest.read_text() + "\nunknown: true\n", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="Additional properties"):
        AgentPackageLoader().load(root)


def test_registry_loads_current_agent_packages():
    registry, _, _ = _package_registry()

    assert {(item.manifest.metadata.name, item.manifest.metadata.version) for item in registry.list()} == {
        ("link", "2.0.0"),
        ("mmchat", "2.1.0"),
        ("ppt", "3.1.1"),
        ("project", "2.0.1"),
        ("report", "2.1.1"),
    }
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

    assert request.system_prompt.startswith("You are MMAG's governed Project Agent.")
    assert "conversation-agent prompt" not in request.system_prompt


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
        DeepAgentProvider(ModelGateway({"default": StubRuntime()}), capabilities, model_policies),
        DirectAgentProvider(capabilities, executor),
    )

    registry = AgentRegistry(factory.create_all(packages.list()))

    assert {agent.descriptor.name for agent in registry.list()} == {
        "link",
        "mmchat",
        "ppt",
        "project",
        "report",
    }
    assert registry.default().descriptor.name == "mmchat"
