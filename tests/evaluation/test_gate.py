from pathlib import Path
from types import MappingProxyType

import pytest

from mmag.agent_packages import AgentPackageRegistry
from mmag.control_plane import SQLiteControlPlane
from mmag.evaluation import PackageActivationError, PackageActivationGate
from mmag.execution import ExecutionProfileRegistry
from mmag.governance import ModelPolicyRegistry, PolicyRegistry
from mmag.skill_packages import SkillPackageRegistry
from mmag.skill_packages.models import SkillEvalAsset

ROOT = Path(__file__).resolve().parents[2]


def test_package_gate_records_only_fully_validated_registry_batches(tmp_path):
    store = SQLiteControlPlane(tmp_path / "releases.db")
    gate = PackageActivationGate(store.releases)
    skills = SkillPackageRegistry(activation_gate=gate)
    skills.load_directory(ROOT / "skills")
    policies = PolicyRegistry()
    policies.load_directory(ROOT / "policies")
    model_policies = ModelPolicyRegistry()
    model_policies.load_directory(ROOT / "model-policies")
    profiles = ExecutionProfileRegistry()
    profiles.load_directory(ROOT / "execution-profiles")
    agents = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=model_policies,
        skill_registry=skills,
        execution_profile_registry=profiles,
        activation_gate=gate,
    )
    agents.load_directory(ROOT / "agents")

    assert len(store.releases.list_active("skill")) == 4
    assert len(store.releases.list_active("agent")) == 5
    assert all(record["package_hash"] for record in store.releases.list_active())
    store.close()


def test_failed_gate_does_not_publish_release_record(tmp_path):
    from dataclasses import replace

    store = SQLiteControlPlane(tmp_path / "releases.db")
    gate = PackageActivationGate(store.releases)
    skills = SkillPackageRegistry()
    packages = skills.load_directory(ROOT / "skills")
    invalid = replace(
        packages[0],
        evals=MappingProxyType(
            {
                "evals.yml": SkillEvalAsset(
                    "evals.yml",
                    "1",
                    (
                        MappingProxyType(
                            {
                                "id": "false-negative",
                                "input": {
                                    "intent": "test",
                                    "goal": "valid input",
                                    "parameters": {},
                                },
                                "expect_error": "invalid_input",
                            }
                        ),
                    ),
                    "0" * 64,
                )
            }
        ),
    )

    with pytest.raises(PackageActivationError, match="expects an error for valid input"):
        gate.activate_skills((invalid,))

    assert store.releases.list_active() == []
    store.close()
