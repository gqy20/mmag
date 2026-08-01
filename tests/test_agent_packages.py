import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mmag.agent_packages import (
    AgentFactory,
    AgentPackageLoader,
    AgentPackageRegistry,
    AgentProviderRegistry,
    ContractAgentDecorator,
    InvalidAgentOutputError,
    LangGraphJSONProvider,
    LangGraphTextProvider,
    ManifestValidationError,
    PackageAgentRunner,
    SingleCapabilityProvider,
)
from mmag.agent_system import AgentDescriptor, AgentRegistry, AgentRequest, RuntimeAgent
from mmag.capabilities import (
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    bind_langgraph_capability,
    build_builtin_bindings,
)
from mmag.execution import ExecutionProfileRegistry
from mmag.governance import (
    ModelGateway,
    ModelPolicyRegistry,
    PolicyRegistry,
    RegistryPolicyAuthorizer,
)
from mmag.runtimes import AgentResult, RunContext, RunRequest
from mmag.skill_packages import SkillPackageRegistry

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agents" / "link"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _skill_registry() -> SkillPackageRegistry:
    registry = SkillPackageRegistry()
    registry.load_directory(REPOSITORY_ROOT / "skills")
    return registry


def _execution_profiles() -> ExecutionProfileRegistry:
    registry = ExecutionProfileRegistry()
    registry.load_directory(REPOSITORY_ROOT / "execution-profiles")
    return registry


def test_loader_compiles_flat_manifest_execution_prompts_schemas_and_version():
    package = AgentPackageLoader().load(PACKAGE_ROOT)

    assert package.manifest.metadata.name == "link"
    assert package.snapshot.agent_spec_version == "1.2.0"
    assert package.snapshot.prompt_version == "1.2.0"
    assert package.snapshot.input_schema_version == "1.0.0"
    assert len(package.snapshot.package_hash) == 64
    assert len(package.snapshot.eval_hash) == 64
    assert package.evals
    assert set(package.manifest.capabilities.allow) == {"analyze_link"}
    assert package.manifest.execution.provider == "single-v1"
    assert package.manifest.routing.requires_url is True


def test_loader_rejects_unknown_manifest_fields(tmp_path):
    root = tmp_path / "link-agent"
    shutil.copytree(PACKAGE_ROOT, root)
    manifest = root / "agent.yml"
    manifest.write_text(manifest.read_text() + "\nunknown: true\n", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="Additional properties"):
        AgentPackageLoader().load(root)


def test_registry_rejects_directory_name_that_differs_from_manifest(tmp_path):
    root = tmp_path / "agents" / "link-agent"
    shutil.copytree(PACKAGE_ROOT, root)

    with pytest.raises(ManifestValidationError, match="does not match manifest"):
        AgentPackageRegistry().load_directory(tmp_path / "agents")


def test_registry_loads_flat_packages_and_resolves_governance_hashes():
    policies = PolicyRegistry()
    policies.load_directory(REPOSITORY_ROOT / "policies")
    model_policies = ModelPolicyRegistry()
    model_policies.load_directory(REPOSITORY_ROOT / "model-policies")
    registry = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=model_policies,
        skill_registry=_skill_registry(),
        execution_profile_registry=_execution_profiles(),
    )

    loaded = registry.load_directory(REPOSITORY_ROOT / "agents")

    assert {(item.manifest.metadata.name, item.manifest.metadata.version) for item in loaded} == {
        ("link", "1.2.0"),
        ("mmchat", "1.2.0"),
        ("ppt", "2.2.0"),
        ("project", "1.0.0"),
        ("report", "1.0.0"),
    }
    snapshot = registry.get("mmchat").snapshot
    assert len(snapshot.policy_hash) == 64
    assert len(snapshot.model_policy_hash) == 64
    assert len(snapshot.skill_set_hash) == 64
    assert tuple(package.manifest.metadata.name for package in registry.list()) == (
        "link",
        "mmchat",
        "ppt",
        "project",
        "report",
    )


@pytest.mark.asyncio
async def test_mmchat_runtime_is_enforced_by_its_package():
    policies = PolicyRegistry()
    policies.load_directory(REPOSITORY_ROOT / "policies")
    model_policies = ModelPolicyRegistry()
    model_policies.load_directory(REPOSITORY_ROOT / "model-policies")
    registry = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=model_policies,
        skill_registry=_skill_registry(),
        execution_profile_registry=_execution_profiles(),
    )
    registry.load_directory(REPOSITORY_ROOT / "agents")
    package = registry.get("mmchat")
    runtime = SequenceRuntime(["hello"])
    agent = ContractAgentDecorator(
        package,
        RuntimeAgent(
            AgentDescriptor(
                "mmchat",
                "mmchat",
                intents=("chat",),
                capabilities=("get_posts", "mcp_docs_search"),
                scopes=("mattermost:*",),
            ),
            runtime,
        ),
    )
    runtime_request = RunRequest(
        RunContext("trace-1", "user-1", "channel-1", "mattermost:team-1/channel-1"),
        ({"role": "user", "content": "hi"},),
        max_rounds=99,
    )

    result = await agent.run(
        AgentRequest(
            intent="chat",
            prompt="hi",
            actor_id="user-1",
            scope="mattermost:team-1/channel-1",
            runtime_request=runtime_request,
        )
    )

    assert result.text == "hello"
    assert result.runtime_result is not None
    assert result.envelope["provenance"]["policy_hash"] == package.snapshot.policy_hash
    assert runtime.requests[0].max_rounds == package.manifest.runtime.max_turns
    assert runtime.requests[0].context.deadline is not None


@pytest.mark.asyncio
async def test_single_capability_provider_is_enforced_by_package_contracts():
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
    policies.load_directory(REPOSITORY_ROOT / "policies")
    executor = CapabilityExecutor(RegistryPolicyAuthorizer(policies))
    capabilities = CapabilityRegistry()
    capabilities.register(bind_langgraph_capability(capability, executor=executor))
    agent = SingleCapabilityProvider(capabilities, executor).create(package)

    result = await agent.run(
        AgentRequest(intent="link", prompt="https://example.com", actor_id="user-1")
    )

    assert result.envelope is not None
    assert result.envelope["status"] == "succeeded"
    assert result.envelope["provenance"]["package_hash"] == package.snapshot.package_hash
    assert result.envelope["result"]["title"] == "Example"


def test_factory_rejects_unknown_yaml_provider():
    package = AgentPackageLoader().load(PACKAGE_ROOT)

    with pytest.raises(Exception, match="unknown Agent execution provider"):
        AgentFactory(AgentProviderRegistry()).create(package)


def test_factory_auto_constructs_every_declared_agent():
    policies = PolicyRegistry()
    policies.load_directory(REPOSITORY_ROOT / "policies")
    model_policies = ModelPolicyRegistry()
    model_policies.load_directory(REPOSITORY_ROOT / "model-policies")
    packages = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=model_policies,
        skill_registry=_skill_registry(),
        execution_profile_registry=_execution_profiles(),
    )
    packages.load_directory(REPOSITORY_ROOT / "agents")
    executor = CapabilityExecutor(RegistryPolicyAuthorizer(policies))
    capabilities = CapabilityRegistry()
    for binding in build_builtin_bindings(MagicMock(), MagicMock(), executor=executor):
        capabilities.register(binding)
    for name in ("ppt.build", "ppt.shell"):
        capability = CapabilitySpec(
            name=name,
            description=name,
            input_schema={"type": "object"},
            handler=lambda: {},
            permission="artifact:generate",
        )
        capabilities.register(bind_langgraph_capability(capability, executor=executor))
    gateway = ModelGateway({"default": SequenceRuntime(["unused"])})
    providers = AgentProviderRegistry()
    providers.register(LangGraphTextProvider(gateway, capabilities, model_policies))
    providers.register(LangGraphJSONProvider(gateway, capabilities))
    providers.register(SingleCapabilityProvider(capabilities, executor))

    registry = AgentRegistry(AgentFactory(providers).create_all(packages.list()))

    assert {agent.descriptor.name for agent in registry.list()} == {
        "link",
        "mmchat",
        "ppt",
        "project",
        "report",
    }
    assert registry.default().descriptor.name == "mmchat"


class SequenceRuntime:
    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return AgentResult(text=self.outputs.pop(0), runtime="stub")


@pytest.mark.asyncio
async def test_runtime_package_repairs_invalid_json_once_and_removes_tools():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    runtime = SequenceRuntime(["not-json", json.dumps({"summary": "ok"})])
    agent = PackageAgentRunner(
        package,
        runtime,
        {"analyze_link": {"name": "analyze_link", "input_schema": {"type": "object"}}},
    )

    result = await agent.run(AgentRequest(intent="link", prompt="https://example.com"))

    assert result.structured_result == {"summary": "ok"}
    assert len(runtime.requests) == 2
    assert runtime.requests[0].capabilities
    assert runtime.requests[1].capabilities == ()
    repair_prompt = runtime.requests[1].messages[0]["content"]
    assert "Exact required JSON Schema" in repair_prompt
    assert '{"type": "object"}' in repair_prompt
    assert result.envelope["usage"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_runtime_package_narrows_channel_argument_to_trusted_conversation():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    capability_spec = CapabilitySpec(
        name="analyze_link",
        description="analyze",
        input_schema={
            "type": "object",
            "properties": {"channel_id": {"type": "string"}},
        },
        handler=lambda: {},
    )
    capability = {
        "name": "analyze_link",
        "input_schema": capability_spec.input_schema,
    }
    runtime = SequenceRuntime([json.dumps({"summary": "ok"})])
    agent = PackageAgentRunner(package, runtime, {"analyze_link": capability})

    await agent.run(
        AgentRequest(
            intent="link",
            prompt="https://example.com",
            scope="mattermost:team-1/channel-1",
        )
    )

    projected = runtime.requests[0].capabilities[0]["input_schema"]["properties"]["channel_id"]
    assert projected["const"] == "channel-1"
    assert "const" not in capability_spec.input_schema["properties"]["channel_id"]
    assert runtime.requests[0].context.conversation_id == "channel-1"


@pytest.mark.asyncio
async def test_runtime_package_returns_stable_invalid_output_after_one_repair():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    runtime = SequenceRuntime(["bad", "still bad"])
    agent = PackageAgentRunner(
        package,
        runtime,
        {"analyze_link": {"name": "analyze_link", "input_schema": {"type": "object"}}},
    )

    with pytest.raises(InvalidAgentOutputError) as raised:
        await agent.run(AgentRequest(intent="link", prompt="https://example.com"))

    assert raised.value.code == "INVALID_OUTPUT"
    assert len(runtime.requests) == 2
