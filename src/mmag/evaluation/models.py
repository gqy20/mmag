"""Immutable contracts for versioned MMAG evaluation assets and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class EvaluationActorRef:
    username_env: str
    password_env: str


@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    id: str
    version: str
    driver: str
    base_url_env: str
    channel_id_env: str
    actors: Mapping[str, EvaluationActorRef]
    bot_username_env: str = ""
    enabled_env: str = "MMAG_E2E_ENABLED"
    control_plane_db_env: str = ""
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 1.0
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "actors", MappingProxyType(dict(self.actors)))


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    id: str
    version: str
    actor: str
    message: str
    expected: Mapping[str, Any]
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected", _freeze_mapping(self.expected))
        object.__setattr__(self, "thresholds", _freeze_mapping(self.thresholds))


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    id: str
    version: str
    case_refs: tuple[str, ...]
    repetitions: int = 1
    pass_policy: Mapping[str, Any] = field(default_factory=dict)
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pass_policy", _freeze_mapping(self.pass_policy))


@dataclass(frozen=True, slots=True)
class ControlPlaneObservation:
    agent_run_state: str = ""
    task_state: str = ""
    delivery_states: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    root_post_id: str
    run_id: str
    response_text: str = ""
    response_kind: str = ""
    terminal_status: str = ""
    thread_consistent: bool = True
    approval_seen: bool = False
    approval_id: str = ""
    approval_decision_denied: bool = False
    artifact_count: int = 0
    duration_seconds: float = 0.0
    timed_out: bool = False
    post_ids: tuple[str, ...] = ()
    control_plane: ControlPlaneObservation = field(default_factory=ControlPlaneObservation)


@dataclass(frozen=True, slots=True)
class EvaluationAssertion:
    name: str
    passed: bool
    expected: Any
    actual: Any
    severity: str = "functional"


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    case_id: str
    case_version: str
    scenario_sha256: str
    repetition: int
    passed: bool
    assertions: tuple[EvaluationAssertion, ...]
    duration_seconds: float
    run_id: str = ""
    root_post_id: str = ""
    response_sha256: str = ""
    response_excerpt: str = ""
    error_code: str = ""
    error_message: str = ""

    @property
    def security_violations(self) -> int:
        return sum(
            1
            for assertion in self.assertions
            if assertion.severity == "security" and not assertion.passed
        )


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    id: str
    suite_id: str
    suite_version: str
    suite_sha256: str
    profile_id: str
    profile_version: str
    profile_sha256: str
    started_at: str
    completed_at: str
    passed: bool
    cases: tuple[EvaluationCaseResult, ...]
    functional_success_rate: float
    security_violation_count: int
    report_path: str = ""
