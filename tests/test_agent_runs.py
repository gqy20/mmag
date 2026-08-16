import pytest

from mmag.control_plane import (
    AgentRunConflictError,
    AgentRunService,
    AgentRunSpec,
    AgentRunState,
    EntityType,
    InvalidTransitionError,
    SQLiteControlPlane,
    delegation_execution_key,
)


def _spec(run_id: str, **changes) -> AgentRunSpec:
    values = {
        "run_id": run_id,
        "workflow_id": "workflow-1",
        "actor_id": "user-1",
        "scope_id": "scope-1",
        "trace_id": "trace-1",
        "thread_id": run_id,
        "agent_ref": "research@1.0.0",
        "package_snapshot": {"package_hash": "hash-1", "policy_ref": "default@1.0.0"},
    }
    values.update(changes)
    return AgentRunSpec(**values)


def test_child_run_creation_is_replay_idempotent(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    runs = AgentRunService(store)
    parent, created = runs.create_or_get(_spec("parent-run"))
    child_spec = _spec(
        "child-run-1",
        parent_run_id=parent.run_id,
        parent_tool_call_id="tool-call-1",
        agent_ref="task-agent@2.0.0",
        skill_ref="task-extraction@1.0.0",
        package_snapshot={"policy_ref": "default@1.0.0", "package_hash": "hash-2"},
    )

    child, child_created = runs.create_or_get(child_spec)
    replay, replay_created = runs.create_or_get(
        _spec(
            "different-candidate-run",
            thread_id="different-candidate-run",
            parent_run_id=parent.run_id,
            parent_tool_call_id="tool-call-1",
            agent_ref="task-agent@2.0.0",
            skill_ref="task-extraction@1.0.0",
            package_snapshot={"package_hash": "hash-2", "policy_ref": "default@1.0.0"},
        )
    )

    assert created is True
    assert child_created is True
    assert replay_created is False
    assert replay == child
    assert replay.run_id == "child-run-1"
    assert replay.thread_id == "child-run-1"
    assert replay.execution_key == delegation_execution_key(child_spec)
    assert store.runs.find_by_execution_key(child.execution_key) == child
    created_audits = store.list_audits(event_type="lifecycle.agent_run.created")
    assert {audit.target for audit in created_audits} == {"parent-run", "child-run-1"}
    child_audit = next(audit for audit in created_audits if audit.target == "child-run-1")
    assert child_audit.actor_id == "user-1"
    assert child_audit.scope_id == "scope-1"
    assert child_audit.trace_id == "trace-1"
    assert child_audit.details["parent_run_id"] == "parent-run"
    assert child_audit.details["agent_ref"] == "task-agent@2.0.0"
    assert "snapshot" not in child_audit.details
    store.close()


def test_child_run_rejects_missing_parent_and_cross_scope_identity(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    runs = AgentRunService(store)
    runs.create_or_get(_spec("parent-run"))

    with pytest.raises(AgentRunConflictError, match="does not exist"):
        runs.create_or_get(
            _spec(
                "orphan",
                parent_run_id="missing",
                parent_tool_call_id="tool-1",
            )
        )
    with pytest.raises(AgentRunConflictError, match="inherit"):
        runs.create_or_get(
            _spec(
                "cross-scope",
                scope_id="scope-2",
                parent_run_id="parent-run",
                parent_tool_call_id="tool-2",
            )
        )
    store.close()


def test_agent_run_identity_is_immutable_and_state_machine_supports_waiting_child(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    runs = AgentRunService(store)
    run, _ = runs.create_or_get(_spec("run-1"))

    with pytest.raises(AgentRunConflictError, match="identity"):
        runs.create_or_get(_spec("run-1", agent_ref="other@1.0.0"))
    with pytest.raises(AgentRunConflictError, match="thread identity"):
        runs.create_or_get(_spec("run-1", thread_id="other-thread"))

    running = runs.transition(
        run.run_id,
        AgentRunState.RUNNING,
        command_id="start-run-1",
        expected_version=run.version,
    )
    waiting = runs.transition(
        run.run_id,
        AgentRunState.WAITING_CHILD,
        command_id="wait-child-run-1",
        expected_version=running.version,
    )
    duplicate = runs.transition(
        run.run_id,
        AgentRunState.WAITING_CHILD,
        command_id="wait-child-run-1",
        expected_version=running.version,
    )

    assert waiting == duplicate
    assert waiting.state is AgentRunState.WAITING_CHILD
    with pytest.raises(InvalidTransitionError):
        runs.transition(
            run.run_id,
            AgentRunState.SUCCEEDED,
            command_id="skip-child-run-1",
        )
    store.close()


def test_agent_run_result_and_terminal_state_commit_together(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    runs = AgentRunService(store)
    queued, _ = runs.create_or_get(_spec("run-atomic"))
    running = runs.transition(
        queued.run_id,
        AgentRunState.RUNNING,
        command_id="start-atomic",
        expected_version=queued.version,
    )

    completed = runs.finish(
        running.run_id,
        AgentRunState.SUCCEEDED,
        result_envelope={"status": "succeeded", "result": {"value": 1}},
        command_id="finish-atomic",
        expected_version=running.version,
        actor_id="user-1",
        trace_id="trace-1",
    )

    assert completed.state is AgentRunState.SUCCEEDED
    assert completed.result_envelope == {"status": "succeeded", "result": {"value": 1}}
    assert [
        item.to_state
        for item in store.list_transitions(EntityType.AGENT_RUN, completed.run_id)
    ] == ["running", "succeeded"]
    assert store.get_lifecycle_entity(EntityType.AGENT_RUN, completed.run_id).payload[
        "dispatch_result"
    ] == completed.result_envelope
    store.close()
