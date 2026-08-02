"""Evaluation suite orchestration independent from any Agent Runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from ..governance import redact_sensitive
from .assertions import DeterministicEvaluator
from .models import (
    EvaluationAssertion,
    EvaluationCaseResult,
    EvaluationObservation,
    EvaluationProfile,
    EvaluationRunResult,
    EvaluationScenario,
)

if TYPE_CHECKING:
    from .loader import EvaluationAssetLoader
    from .reporting import JSONEvaluationReporter


class EvaluationDriver(Protocol):
    async def execute(
        self,
        scenario: EvaluationScenario,
        profile: EvaluationProfile,
        evaluation_run_id: str,
    ) -> EvaluationObservation: ...


class EvaluationRunner:
    def __init__(
        self,
        loader: EvaluationAssetLoader,
        driver: EvaluationDriver,
        reporter: JSONEvaluationReporter,
        *,
        evaluator: DeterministicEvaluator | None = None,
    ) -> None:
        self.loader = loader
        self.driver = driver
        self.reporter = reporter
        self.evaluator = evaluator or DeterministicEvaluator()

    async def run(self, suite_ref: str, profile_ref: str) -> EvaluationRunResult:
        suite = self.loader.load_suite(suite_ref)
        profile = self.loader.load_profile(profile_ref)
        scenarios = self.loader.load_suite_cases(suite)
        run_id = f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
        started_at = datetime.now(UTC).isoformat()
        results: list[EvaluationCaseResult] = []
        for repetition in range(1, suite.repetitions + 1):
            for scenario in scenarios:
                results.append(await self._run_case(scenario, profile, run_id, repetition))

        functional_success_rate = (
            sum(1 for result in results if result.passed) / len(results) if results else 0.0
        )
        security_violations = sum(result.security_violations for result in results)
        minimum_success = float(suite.pass_policy.get("functional_success_rate", 1.0))
        maximum_security = int(suite.pass_policy.get("security_violation_count", 0))
        report_path = str(self.reporter.path_for(run_id))
        result = EvaluationRunResult(
            id=run_id,
            suite_id=suite.id,
            suite_version=suite.version,
            suite_sha256=suite.sha256,
            profile_id=profile.id,
            profile_version=profile.version,
            profile_sha256=profile.sha256,
            started_at=started_at,
            completed_at=datetime.now(UTC).isoformat(),
            passed=(
                bool(results)
                and functional_success_rate >= minimum_success
                and security_violations <= maximum_security
            ),
            cases=tuple(results),
            functional_success_rate=functional_success_rate,
            security_violation_count=security_violations,
            report_path=report_path,
        )
        self.loader.validate_result(json.loads(json.dumps(asdict(result), default=str)))
        self.reporter.write(result)
        return result

    async def _run_case(
        self,
        scenario: EvaluationScenario,
        profile: EvaluationProfile,
        evaluation_run_id: str,
        repetition: int,
    ) -> EvaluationCaseResult:
        started = time.monotonic()
        try:
            observation = await self.driver.execute(scenario, profile, evaluation_run_id)
            assertions = self.evaluator.evaluate(scenario, observation)
            return EvaluationCaseResult(
                case_id=scenario.id,
                case_version=scenario.version,
                scenario_sha256=scenario.sha256,
                repetition=repetition,
                passed=bool(assertions) and all(assertion.passed for assertion in assertions),
                assertions=assertions,
                duration_seconds=observation.duration_seconds,
                run_id=observation.run_id,
                root_post_id=observation.root_post_id,
                response_sha256=hashlib.sha256(
                    observation.response_text.encode("utf-8")
                ).hexdigest(),
                response_excerpt=self._excerpt(observation.response_text),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = redact_sensitive(str(error))
            return EvaluationCaseResult(
                case_id=scenario.id,
                case_version=scenario.version,
                scenario_sha256=scenario.sha256,
                repetition=repetition,
                passed=False,
                assertions=(
                    EvaluationAssertion("execution_completed", False, True, False),
                ),
                duration_seconds=time.monotonic() - started,
                error_code=type(error).__name__,
                error_message=message[:1_000],
            )

    @staticmethod
    def _excerpt(value: str) -> str:
        cleaned = redact_sensitive(value).strip()
        return cleaned if len(cleaned) <= 1_000 else cleaned[:999].rstrip() + "…"
