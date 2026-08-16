import json

from mmag.control_plane import (
    AgentRunService,
    AgentRunSpec,
    AgentRunState,
    ApprovalService,
    Artifact,
    LifecycleService,
    OutboundMessage,
    SQLiteControlPlane,
)
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
                "thread_id": "",
                "agent_ref": "",
                "skill_ref": "",
                "status": "selected",
            "last_event": "delivery.dispatch.completed",
        },
        {
            "run_id": "delegate:project:child-1",
                "parent_run_id": "mattermost:post-1",
                "workflow_id": "mattermost:post-1",
                "thread_id": "",
                "agent_ref": "",
                "skill_ref": "",
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


def test_diagnostic_reader_reports_cross_entity_state_contradictions():
    warnings = DiagnosticReader._graph_warnings(
        (
            {
                "run_id": "parent",
                "parent_run_id": "",
                "status": "waiting_child",
            },
            {
                "run_id": "child",
                "parent_run_id": "parent",
                "status": "succeeded",
            },
        ),
        (
            {
                "capability_call_id": "call-1",
                "status": "running",
            },
        ),
        (
            {
                "approval_id": "approval-1",
                "child_run_id": "child",
                "status": "pending",
            },
        ),
        (),
        (
            {
                "delivery_id": "delivery-1",
                "status": "failed",
                "artifact_refs": [f"artifact://{'b' * 32}"],
            },
        ),
    )

    assert warnings == (
        "waiting_child_without_active_child:parent",
        "capability_call_nonterminal:call-1",
        "approval_without_waiting_run:approval-1:child",
        "delivery_failed:delivery-1",
        f"delivery_missing_artifact:delivery-1:artifact://{'b' * 32}",
    )


def test_diagnostic_reader_restores_control_plane_run_and_approval_graph(tmp_path):
    database = tmp_path / "control.db"
    logs = tmp_path / "logs"
    store = SQLiteControlPlane(database)
    lifecycle = LifecycleService(store)
    runs = AgentRunService(store, lifecycle)
    parent, _ = runs.create_or_get(
        AgentRunSpec(
            run_id="run:event-1",
            workflow_id="mattermost:root-1",
            actor_id="user-1",
            scope_id="scope-1",
            trace_id="trace-1",
            thread_id="mattermost:root-1",
            agent_ref="mmchat@1.0.0",
            package_snapshot={"package_hash": "parent"},
        )
    )
    parent = runs.transition(
        parent.run_id,
        AgentRunState.RUNNING,
        command_id="parent-running",
        expected_version=parent.version,
    )
    child, _ = runs.create_or_get(
        AgentRunSpec(
            run_id="delegate:project:child-1",
            workflow_id=parent.workflow_id,
            parent_run_id=parent.run_id,
            parent_tool_call_id="tool-1",
            actor_id=parent.actor_id,
            scope_id=parent.scope_id,
            trace_id=parent.trace_id,
            thread_id="delegate:project:child-1",
            agent_ref="project@1.0.0",
            package_snapshot={"package_hash": "child"},
        )
    )
    child = runs.transition(
        child.run_id,
        AgentRunState.RUNNING,
        command_id="child-running",
        expected_version=child.version,
    )
    runs.transition(
        child.run_id,
        AgentRunState.WAITING_APPROVAL,
        command_id="child-waiting",
        expected_version=child.version,
    )
    runs.transition(
        parent.run_id,
        AgentRunState.WAITING_CHILD,
        command_id="parent-waiting",
        expected_version=parent.version,
    )
    approval = ApprovalService(store, lifecycle).request(
        "create_task",
        {
            "thread_id": "mattermost:root-1",
            "capability_context": {
                "trace_id": "trace-1",
                "lifecycle_run_id": parent.run_id,
            },
            "delegated_child": {
                "run_id": child.run_id,
                "resume": {
                    "thread_id": child.run_id,
                    "runtime_snapshot": {"context": {"trace_id": "trace-1"}},
                },
            },
        },
        requested_by="user-1",
        scope_id="scope-1",
    )
    for decision, details in (
        ("running", {}),
        ("succeeded", {"duration_ms": 12}),
    ):
        store.append_audit(
            "runtime.tool.call",
            actor_id="user-1",
            scope_id="scope-1",
            trace_id="trace-1",
            target="create_task",
            decision=decision,
            details={
                "run_id": child.run_id,
                "parent_run_id": parent.run_id,
                "runtime_call_id": "call-1",
                "parent_runtime_call_id": "model-1",
                **details,
            },
        )
    artifact_id = "a" * 32
    store.create_artifact(
        Artifact(
            artifact_id,
            child.run_id,
            "scope-1",
            "application/json",
            "private/path.json",
            {"size_bytes": 42, "schema_version": "1.0", "secret": "hidden"},
        )
    )
    delivery_id = store.enqueue_delivery(
        OutboundMessage(
            conversation_id="conversation-1",
            text="private result",
            agent_run_id=child.run_id,
            idempotency_key="diagnostic-delivery-1",
            scope_id="scope-1",
            artifact_refs=(f"artifact://{artifact_id}",),
            actor_id="user-1",
        )
    )
    store.close()

    by_child = DiagnosticReader(database, logs).report(child.run_id)
    by_approval = DiagnosticReader(database, logs).report(approval.id)
    by_call = DiagnosticReader(database, logs).report("call-1")
    by_artifact = DiagnosticReader(database, logs).report(artifact_id)
    by_delivery = DiagnosticReader(database, logs).report(delivery_id)

    assert {
        by_child.trace_id,
        by_approval.trace_id,
        by_call.trace_id,
        by_artifact.trace_id,
        by_delivery.trace_id,
    } == {"trace-1"}
    assert by_child.run_graph == (
        {
            "run_id": parent.run_id,
            "parent_run_id": "",
            "workflow_id": "mattermost:root-1",
            "thread_id": "mattermost:root-1",
            "agent_ref": "mmchat@1.0.0",
            "skill_ref": "",
            "status": "waiting_child",
            "last_event": "control_plane.agent_run",
        },
        {
            "run_id": child.run_id,
            "parent_run_id": parent.run_id,
            "workflow_id": "mattermost:root-1",
            "thread_id": child.run_id,
            "agent_ref": "project@1.0.0",
            "skill_ref": "",
            "status": "waiting_approval",
            "last_event": "runtime.tool.call",
        },
    )
    assert by_child.approvals == by_approval.approvals == (
        {
            "approval_id": approval.id,
            "capability": "create_task",
            "status": "pending",
            "thread_id": "mattermost:root-1",
            "child_run_id": child.run_id,
        },
    )
    assert by_child.capability_calls == (
        {
            "capability_call_id": "call-1",
            "run_id": child.run_id,
            "parent_run_id": parent.run_id,
            "capability": "create_task",
            "status": "succeeded",
            "parent_capability_call_id": "model-1",
            "duration_ms": 12,
            "error_code": "",
        },
    )
    assert by_child.artifacts == (
        {
            "artifact_id": artifact_id,
            "ref": f"artifact://{artifact_id}",
            "run_id": child.run_id,
            "kind": "application/json",
            "size_bytes": 42,
            "schema_version": "1.0",
        },
    )
    assert by_child.deliveries == (
        {
            "delivery_id": delivery_id,
            "run_id": child.run_id,
            "status": "pending",
            "message_kind": "result",
            "attempts": 0,
            "artifact_refs": [f"artifact://{artifact_id}"],
            "remote_delivered": False,
            "has_error": False,
        },
    )
    assert "private" not in str(by_child.to_dict())
    assert "hidden" not in str(by_child.to_dict())
    assert by_child.warnings == ()
