import json
import shutil
from pathlib import Path

import pytest

from mmag.agent_packages import (
    AgentPackageLoader,
    AgentPackageRegistry,
    ContractManagedAgent,
    InvalidAgentOutputError,
    ManifestValidationError,
    RuntimePackageAgent,
)
from mmag.capabilities import CapabilityExecutor, CapabilitySpec
from mmag.governance import PolicyCapabilityAuthorizer, PolicyEffect, PolicyEngine, PolicyRule
from mmag.managed_agents import AgentRouteRequest, LinkAgent
from mmag.runtimes import AgentResult

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agents" / "link-agent"


def test_loader_compiles_manifest_prompts_schemas_and_versions():
    package = AgentPackageLoader().load(PACKAGE_ROOT)

    assert package.manifest.metadata.name == "link"
    assert package.snapshot.agent_spec_version == "1.0.0"
    assert package.snapshot.prompt_version == "v1"
    assert package.snapshot.input_schema_version == "1.0.0"
    assert len(package.snapshot.package_hash) == 64
    assert set(package.manifest.capabilities.allow) == {"analyze_link"}


def test_loader_rejects_unknown_manifest_fields(tmp_path):
    root = tmp_path / "link-agent"
    shutil.copytree(PACKAGE_ROOT, root)
    manifest = root / "agent.yml"
    manifest.write_text(manifest.read_text() + "\nunknown: true\n", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="Additional properties"):
        AgentPackageLoader().load(root)


def test_registry_keeps_versions_and_rejects_mutating_a_published_version(tmp_path):
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    registry = AgentPackageRegistry()
    registry.publish(package)
    root = tmp_path / "link-agent"
    shutil.copytree(PACKAGE_ROOT, root)
    prompt = root / "prompts/v1/system.md"
    prompt.write_text(prompt.read_text() + "\nChanged.\n", encoding="utf-8")

    changed = AgentPackageLoader().load(root)
    with pytest.raises(ValueError, match="immutable"):
        registry.publish(changed)


@pytest.mark.asyncio
async def test_link_agent_is_enforced_by_package_input_output_and_artifact_contracts():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    capability = CapabilitySpec(
        name="analyze_link",
        description="analyze",
        input_schema={"type": "object"},
        handler=lambda url: {"url": url, "title": "Example"},
    )
    policy = PolicyEngine(
        (PolicyRule("allow-owner", PolicyEffect.ALLOW, actors=("user-1",)),)
    )
    executor = CapabilityExecutor(PolicyCapabilityAuthorizer(policy))
    agent = ContractManagedAgent(package, LinkAgent(capability, executor))

    result = await agent.run(
        AgentRouteRequest(intent="link", prompt="https://example.com", actor_id="user-1")
    )

    assert result.envelope is not None
    assert result.envelope["status"] == "succeeded"
    assert result.envelope["provenance"]["package_hash"] == package.snapshot.package_hash
    assert result.envelope["result"]["title"] == "Example"


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
    agent = RuntimePackageAgent(
        package,
        runtime,
        {"analyze_link": {"name": "analyze_link", "input_schema": {"type": "object"}}},
    )

    result = await agent.run(AgentRouteRequest(intent="link", prompt="https://example.com"))

    assert result.structured_result == {"summary": "ok"}
    assert len(runtime.requests) == 2
    assert runtime.requests[0].capabilities
    assert runtime.requests[1].capabilities == ()
    assert result.envelope["usage"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_runtime_package_returns_stable_invalid_output_after_one_repair():
    package = AgentPackageLoader().load(PACKAGE_ROOT)
    runtime = SequenceRuntime(["bad", "still bad"])
    agent = RuntimePackageAgent(
        package,
        runtime,
        {"analyze_link": {"name": "analyze_link", "input_schema": {"type": "object"}}},
    )

    with pytest.raises(InvalidAgentOutputError) as raised:
        await agent.run(AgentRouteRequest(intent="link", prompt="https://example.com"))

    assert raised.value.code == "INVALID_OUTPUT"
    assert len(runtime.requests) == 2
