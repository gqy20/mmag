import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from mmag.control_plane import SQLiteControlPlane
from mmag.evaluation import (
    EvaluationAssetLoader,
    EvaluationRunner,
    JSONEvaluationReporter,
    MattermostEvaluationDriver,
    SQLiteEvaluationObserver,
)


def test_sqlite_observer_resolves_agent_by_original_message_id(tmp_path):
    database = tmp_path / "control.db"
    store = SQLiteControlPlane(database)
    store.append_audit(
        "agent.run",
        target="mmchat",
        details={"message_id": "post-1", "run_id": "mattermost:post-1"},
    )
    store.append_audit(
        "agent.run",
        target="project",
        details={"message_id": "post-2", "run_id": "mattermost:post-2"},
    )
    store.close()

    observation = SQLiteEvaluationObserver().observe(str(database), "post-1")

    assert observation.agent_name == "mmchat"


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
