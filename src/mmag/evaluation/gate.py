"""Offline activation gate for versioned Agent and Skill Packages."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from ..agent_packages.models import AgentPackage
    from ..control_plane.releases import ReleaseStore
    from ..skill_packages.models import SkillPackage


class PackageActivationError(RuntimeError):
    pass


class PackageActivationGate:
    """Validate a complete Registry batch before recording and exposing it."""

    version = "1.0.0"

    def __init__(self, releases: ReleaseStore, *, released_by: str = "system:startup") -> None:
        self.releases = releases
        self.released_by = released_by

    def activate_agents(self, packages: Iterable[AgentPackage]) -> None:
        records = tuple(self._agent_record(package) for package in packages)
        self.releases.activate(records)

    def activate_skills(self, packages: Iterable[SkillPackage]) -> None:
        records = tuple(self._skill_record(package) for package in packages)
        self.releases.activate(records)

    def _agent_record(self, package: AgentPackage) -> dict[str, Any]:
        checks = ["registry_validated"]
        case_count = 0
        for eval_asset in package.evals.values():
            for case in eval_asset.cases:
                self._check_agent_case(package, case)
                case_count += 1
        if case_count:
            checks.append(f"contract_cases:{case_count}")
        return self._record(
            "agent",
            package.manifest.metadata.name,
            package.manifest.metadata.version,
            package.snapshot.package_hash,
            package.snapshot.eval_hash,
            checks,
        )

    def _skill_record(self, package: SkillPackage) -> dict[str, Any]:
        checks = ["registry_validated"]
        case_count = 0
        input_schema = package.schemas[package.manifest.input_schema_ref].schema
        output_schema = package.schemas[package.manifest.output_schema_ref].schema
        for eval_asset in package.evals.values():
            for case in eval_asset.cases:
                self._check_skill_case(case, input_schema, output_schema)
                case_count += 1
        if case_count:
            checks.append(f"contract_cases:{case_count}")
        return self._record(
            "skill",
            package.manifest.metadata.name,
            package.manifest.metadata.version,
            package.snapshot.package_hash,
            package.snapshot.eval_hash,
            checks,
        )

    @staticmethod
    def _check_agent_case(package: AgentPackage, case: Mapping[str, Any]) -> None:
        if case["intent"] not in package.manifest.accepted_intents:
            raise PackageActivationError(
                f"Agent {package.manifest.metadata.name!r} eval intent "
                f"{case['intent']!r} is not accepted"
            )
        expected = case.get("expect")
        if isinstance(expected, Mapping):
            expected_agent = expected.get("agent")
            if expected_agent and expected_agent != package.manifest.metadata.name:
                raise PackageActivationError("Agent eval expects a different Agent")

    @staticmethod
    def _check_skill_case(
        case: Mapping[str, Any],
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> None:
        input_errors = tuple(Draft202012Validator(input_schema).iter_errors(case["input"]))
        if "expect_error" in case:
            if not input_errors:
                raise PackageActivationError(
                    f"Skill eval {case['id']!r} expects an error for valid input"
                )
            return
        if input_errors:
            raise PackageActivationError(f"Skill eval {case['id']!r} has invalid input")
        try:
            Draft202012Validator(output_schema).validate(case["expect"])
        except Exception as error:
            raise PackageActivationError(
                f"Skill eval {case['id']!r} has invalid expected output"
            ) from error

    def _record(
        self,
        kind: str,
        name: str,
        version: str,
        package_hash: str,
        eval_hash: str,
        checks: list[str],
    ) -> dict[str, Any]:
        return {
            "package_kind": kind,
            "package_name": name,
            "package_version": version,
            "package_hash": package_hash,
            "eval_hash": eval_hash,
            "gate_version": self.version,
            "checks": checks,
            "released_by": self.released_by,
        }
