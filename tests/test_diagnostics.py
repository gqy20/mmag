import json

from mmag.control_plane import SQLiteControlPlane
from mmag.diagnostics import DiagnosticReader, resolve_project_path


def test_diagnostic_reader_resolves_exact_run_and_aggregates_rotated_logs(tmp_path):
    database = tmp_path / "control.db"
    logs = tmp_path / "logs"
    logs.mkdir()
    store = SQLiteControlPlane(database)
    store.append_audit(
        "agent.route",
        trace_id="trace-exact",
        target="project",
        decision="selected",
        details={"run_id": "mattermost:post-1"},
    )
    store.append_audit(
        "agent.delegated",
        trace_id="trace-exact",
        target="project",
        decision="running",
        details={
            "run_id": "delegate:project:child-1",
            "parent_run_id": "mattermost:post-1",
            "workflow_id": "mattermost:post-1",
        },
    )
    store.append_audit(
        "agent.route",
        trace_id="trace-other",
        target="mmchat",
        decision="selected",
        details={"run_id": "mattermost:post-10"},
    )
    store.close()
    (logs / "mmag-old.log.1").write_text(
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event": "message.accepted", "trace_id": "trace-exact", "run_id": "mattermost:post-1"}) + "\n",
        encoding="utf-8",
    )
    (logs / "mmag-new.log").write_text(
        json.dumps({"timestamp": "2026-01-01T00:00:01Z", "event": "delivery.dispatch.completed", "trace_id": "trace-exact", "run_id": "mattermost:post-1"}) + "\n",
        encoding="utf-8",
    )

    report = DiagnosticReader(database, logs).report("mattermost:post-1")

    assert report.trace_id == "trace-exact"
    assert report.run_ids == ("delegate:project:child-1", "mattermost:post-1")
    assert report.run_graph == (
        {
            "run_id": "mattermost:post-1",
            "parent_run_id": "",
            "workflow_id": "",
            "status": "selected",
            "last_event": "delivery.dispatch.completed",
        },
        {
            "run_id": "delegate:project:child-1",
            "parent_run_id": "mattermost:post-1",
            "workflow_id": "mattermost:post-1",
            "status": "running",
            "last_event": "agent.delegated",
        },
    )
    assert [item["event"] for item in report.logs] == [
        "message.accepted",
        "delivery.dispatch.completed",
    ]
    assert report.audits[0]["details"]["run_id"] == "mattermost:post-1"


def test_resolve_project_path_honors_relative_runtime_configuration(tmp_path):
    assert resolve_project_path("state/control.db", project_root=tmp_path) == (
        tmp_path / "state/control.db"
    ).resolve()
