import json
import os
import sqlite3
from pathlib import Path

import pytest
from dotenv import load_dotenv

from mmag.control_plane import SQLiteControlPlane
from mmag.evaluation import (
    ControlPlaneObservation,
    EvaluationAssetLoader,
    EvaluationObservation,
    EvaluationRunner,
    EvaluationScenario,
    JSONEvaluationReporter,
    MattermostEvaluationDriver,
    SQLiteEvaluationObserver,
)


def test_control_plane_ready_requires_expected_task_and_capabilities():
    scenario = EvaluationScenario(
        id="task",
        version="1.0.0",
        actor="requester",
        message="create task",
        expected={
            "control_plane": {"agent_name": "mmchat", "delivery_states_all": ["delivered"]},
            "capabilities": {"contains_all": ["delegate_project", "create_task"]},
            "tasks": {"minimum_created": 1},
        },
    )
    incomplete = EvaluationObservation(
        root_post_id="post-1",
        run_id="mattermost:post-1",
        control_plane=ControlPlaneObservation(agent_name="mmchat"),
    )

    assert not MattermostEvaluationDriver._control_plane_ready(scenario, incomplete)


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

    observation, tasks = SQLiteEvaluationObserver().observe(str(database), "post-1")

    assert observation.agent_name == "mmchat"
    assert tasks == ()


def test_sqlite_observer_reports_new_business_task_and_capability(tmp_path):
    database = tmp_path / "control.db"
    store = SQLiteControlPlane(database)
    store.append_audit(
        "agent.run",
        actor_id="user-1",
        scope_id="mattermost:i:t:chn:channel-1",
        trace_id="trace-1",
        target="project",
        details={"message_id": "post-1", "run_id": "mattermost:post-1"},
    )
    store.append_audit(
        "policy.decision",
        actor_id="user-1",
        scope_id="mattermost:i:t:chn:channel-1",
        trace_id="trace-1",
        target="create_task",
        decision="allow",
        details={"run_id": "mattermost:post-1"},
    )
    store.close()
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO tasks
        (id, installation_id, tenant_id, title, creator_id, channel_id, scope_id,
         created_at, updated_at, execution_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "task-1",
            "i",
            "t",
            "MMAG-E2E-test",
            "user-1",
            "channel-1",
            "mattermost:i:t:chn:channel-1",
            1,
            1,
            "execution-key",
        ),
    )
    connection.execute(
        """INSERT INTO outbox_deliveries
        (id, conversation_id, channel_id, message, props, status, attempts,
         next_attempt_at, last_error, remote_id, agent_run_id, created_at,
         updated_at, root_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "delivery-1",
            "channel-1",
            "channel-1",
            "done",
            "{}",
            "delivered",
            1,
            0,
            "",
            "bot-post-1",
            "run:post-1",
            1,
            1,
            "post-1",
        ),
    )
    connection.commit()
    connection.close()

    observation, tasks = SQLiteEvaluationObserver().observe(
        str(database),
        "post-1",
        requester_id="user-1",
        channel_id="channel-1",
    )

    assert observation.agent_name == "project"
    assert observation.capability_names == ("create_task",)
    assert observation.delivery_post_ids == ("bot-post-1",)
    assert tasks[0].title == "MMAG-E2E-test"
    assert tasks[0].creator_matches_requester
    assert tasks[0].channel_matches_request
    assert tasks[0].execution_key_present


def test_sqlite_observer_matches_approval_to_originating_thread(tmp_path):
    database = tmp_path / "control.db"
    store = SQLiteControlPlane(database)
    store.close()
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO approval_requests
        (id, capability_name, arguments, resume_token, requested_by, scope_id,
         decided_by, decision_reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "approval-1",
            "send_file",
            json.dumps({"thread_id": "mattermost:post-1"}),
            "interrupt-1",
            "user-1",
            "scope-1",
            "",
            "",
            1,
            1,
        ),
    )
    connection.commit()
    connection.close()

    observer = SQLiteEvaluationObserver()

    assert observer.approval_registered(str(database), "approval-1", "post-1")
    assert not observer.approval_registered(str(database), "approval-1", "post-2")


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
