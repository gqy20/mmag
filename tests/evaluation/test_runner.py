import json
from pathlib import Path

import pytest

from mmag.evaluation import (
    ControlPlaneObservation,
    EvaluationAssetLoader,
    EvaluationObservation,
    EvaluationRunner,
    JSONEvaluationReporter,
)

ROOT = Path(__file__).resolve().parents[2] / "evals"


class FakeDriver:
    async def execute(self, scenario, profile, evaluation_run_id):
        del profile, evaluation_run_id
        return EvaluationObservation(
            root_post_id="post-1",
            run_id="mattermost:post-1",
            response_text="评估链路正常。",
            response_kind="result",
            terminal_status="succeeded",
            thread_consistent=True,
            duration_seconds=1.5,
            post_ids=("reply-1",),
            control_plane=ControlPlaneObservation("succeeded", "succeeded", ("delivered",)),
        )


@pytest.mark.asyncio
async def test_runner_writes_versioned_redacted_report(tmp_path):
    loader = EvaluationAssetLoader(ROOT)
    reporter = JSONEvaluationReporter(tmp_path)

    result = await EvaluationRunner(loader, FakeDriver(), reporter).run(
        "suites/smoke.yml",
        "profiles/staging-mattermost.yml",
    )

    assert result.passed
    assert result.functional_success_rate == 1.0
    payload = json.loads(Path(result.report_path).read_text())
    assert payload["suite_sha256"]
    assert payload["cases"][0]["run_id"] == "mattermost:post-1"


class FailingDriver:
    async def execute(self, scenario, profile, evaluation_run_id):
        del scenario, profile, evaluation_run_id
        raise RuntimeError("password=do-not-store")


@pytest.mark.asyncio
async def test_runner_converts_driver_failure_to_redacted_case_result(tmp_path):
    result = await EvaluationRunner(
        EvaluationAssetLoader(ROOT),
        FailingDriver(),
        JSONEvaluationReporter(tmp_path),
    ).run("suites/smoke.yml", "profiles/staging-mattermost.yml")

    assert not result.passed
    assert result.cases[0].error_code == "RuntimeError"
    assert "do-not-store" not in result.cases[0].error_message
