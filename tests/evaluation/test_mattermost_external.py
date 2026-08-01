import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from mmag.evaluation import (
    EvaluationAssetLoader,
    EvaluationRunner,
    JSONEvaluationReporter,
    MattermostEvaluationDriver,
)


@pytest.mark.external
@pytest.mark.asyncio
async def test_real_user_bot_smoke_flow(tmp_path):
    project = Path(__file__).resolve().parents[2]
    load_dotenv(project / ".env", override=False)
    if os.getenv("MMAG_E2E_ENABLED", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("set MMAG_E2E_ENABLED=1 to allow real Mattermost evaluation")

    result = await EvaluationRunner(
        EvaluationAssetLoader(project / "evals"),
        MattermostEvaluationDriver(allow_external=True),
        JSONEvaluationReporter(tmp_path),
    ).run("suites/smoke.yml", "profiles/staging-mattermost.yml")

    assert result.passed, result.report_path
