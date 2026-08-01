"""Deterministic assertions for user-visible and control-plane observations."""

from __future__ import annotations

import json
from typing import Any

from .models import EvaluationAssertion, EvaluationObservation, EvaluationScenario


class DeterministicEvaluator:
    def evaluate(
        self,
        scenario: EvaluationScenario,
        observation: EvaluationObservation,
    ) -> tuple[EvaluationAssertion, ...]:
        expected = scenario.expected
        assertions: list[EvaluationAssertion] = []
        self._append(assertions, "completed_before_timeout", True, not observation.timed_out)

        response = self._mapping(expected.get("response"))
        if response.get("kind"):
            self._append(
                assertions,
                "response_kind",
                response["kind"],
                observation.response_kind,
            )
        if response.get("terminal_status"):
            self._append(
                assertions,
                "terminal_status",
                response["terminal_status"],
                observation.terminal_status,
            )
        if response.get("thread_required") is not None:
            self._append(
                assertions,
                "thread_consistent",
                bool(response["thread_required"]),
                observation.thread_consistent,
            )
        if response.get("raw_json_forbidden"):
            self._append(
                assertions,
                "raw_json_not_exposed",
                False,
                self._is_raw_json(observation.response_text),
            )
        for value in self._strings(response.get("contains_all")):
            self._append(
                assertions,
                f"response_contains:{value}",
                True,
                value in observation.response_text,
            )
        contains_any = self._strings(response.get("contains_any"))
        if contains_any:
            self._append(
                assertions,
                "response_contains_any",
                contains_any,
                tuple(value for value in contains_any if value in observation.response_text),
            )
            assertions[-1] = EvaluationAssertion(
                assertions[-1].name,
                bool(assertions[-1].actual),
                assertions[-1].expected,
                assertions[-1].actual,
            )
        for value in self._strings(response.get("not_contains")):
            assertions.append(
                EvaluationAssertion(
                    f"response_not_contains:{value}",
                    value not in observation.response_text,
                    f"not {value!r}",
                    value in observation.response_text,
                    "security",
                )
            )

        approval = self._mapping(expected.get("approval"))
        if approval:
            required = bool(approval.get("required"))
            self._append(assertions, "approval_seen", required, observation.approval_seen)
            if approval.get("authorized") is False:
                assertions.append(
                    EvaluationAssertion(
                        "unauthorized_approval_rejected",
                        observation.approval_decision_denied,
                        True,
                        observation.approval_decision_denied,
                        "security",
                    )
                )

        artifacts = self._mapping(expected.get("artifacts"))
        if artifacts.get("minimum") is not None:
            minimum = int(artifacts["minimum"])
            assertions.append(
                EvaluationAssertion(
                    "artifact_count",
                    observation.artifact_count >= minimum,
                    f">={minimum}",
                    observation.artifact_count,
                )
            )

        control_plane = self._mapping(expected.get("control_plane"))
        if control_plane.get("agent_run_state"):
            self._append(
                assertions,
                "agent_run_state",
                control_plane["agent_run_state"],
                observation.control_plane.agent_run_state,
            )
        if control_plane.get("task_state"):
            self._append(
                assertions,
                "task_state",
                control_plane["task_state"],
                observation.control_plane.task_state,
            )

        max_duration = scenario.thresholds.get("max_duration_seconds")
        if max_duration is not None:
            assertions.append(
                EvaluationAssertion(
                    "duration_seconds",
                    observation.duration_seconds <= float(max_duration),
                    f"<={float(max_duration)}",
                    observation.duration_seconds,
                )
            )
        return tuple(assertions)

    @staticmethod
    def _append(
        assertions: list[EvaluationAssertion],
        name: str,
        expected: Any,
        actual: Any,
    ) -> None:
        assertions.append(EvaluationAssertion(name, expected == actual, expected, actual))

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, str))

    @staticmethod
    def _is_raw_json(value: str) -> bool:
        try:
            return isinstance(json.loads(value.strip()), (dict, list))
        except (json.JSONDecodeError, TypeError):
            return False
