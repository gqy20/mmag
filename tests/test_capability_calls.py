from __future__ import annotations

import pytest

from mmag.control_plane import (
    AgentRunService,
    AgentRunSpec,
    CapabilityCallConflictError,
    CapabilityCallService,
    CapabilityCallSpec,
    CapabilityCallState,
    EntityType,
    SQLiteControlPlane,
)


def _parent(store: SQLiteControlPlane) -> None:
    AgentRunService(store).create_or_get(
        AgentRunSpec(
            run_id="run:root",
            workflow_id="workflow:root",
            actor_id="user-1",
            scope_id="scope-1",
            trace_id="trace-1",
            thread_id="thread-1",
            agent_ref="core@1.0.0",
            package_snapshot={"package_hash": "sha256:package"},
        )
    )


def _spec(**changes: str) -> CapabilityCallSpec:
    values = {
        "call_id": "call:one",
        "execution_key": "execution-key-one",
        "capability": "task.create",
        "actor_id": "user-1",
        "scope_id": "scope-1",
        "trace_id": "trace-1",
        "run_id": "run:root",
        "workflow_id": "workflow:root",
        "tool_call_id": "tool-call-1",
        "agent_ref": "core@1.0.0",
        "skill_ref": "meeting-tasks@1.0.0",
        "policy_ref": "default@1.0.0",
        "input_sha256": "a" * 64,
    }
    values.update(changes)
    return CapabilityCallSpec(**values)


def test_capability_call_lifecycle_is_durable_and_replay_safe(tmp_path) -> None:
    store = SQLiteControlPlane(tmp_path / "calls.db")
    _parent(store)
    service = CapabilityCallService(store)

    requested, created = service.create_or_get(_spec())
    replay, replay_created = service.create_or_get(_spec(call_id="call:replayed-id"))

    assert created is True
    assert replay_created is False
    assert replay.call_id == requested.call_id
    running = service.transition(
        requested.call_id,
        CapabilityCallState.RUNNING,
        command_id="call:start",
        expected_version=requested.version,
        actor_id="user-1",
        trace_id="trace-1",
    )
    completed = service.transition(
        running.call_id,
        CapabilityCallState.SUCCEEDED,
        command_id="call:finish",
        expected_version=running.version,
        actor_id="user-1",
        trace_id="trace-1",
        payload_patch={
            "result_payload": {"task_id": "task-1"},
            "tool_content": '{"task_id":"task-1"}',
            "duration_ms": 12,
        },
    )

    assert completed.state is CapabilityCallState.SUCCEEDED
    assert completed.result_payload == {"task_id": "task-1"}
    assert (
        store.get_lifecycle_entity(EntityType.CAPABILITY_CALL, requested.call_id).state
        == "succeeded"
    )
    assert [
        item.to_state
        for item in store.list_transitions(EntityType.CAPABILITY_CALL, requested.call_id)
    ] == ["running", "succeeded"]
    created_audits = store.list_audits(
        event_type="lifecycle.capability_call.created", target=requested.call_id
    )
    transition_audits = store.list_audits(
        event_type="lifecycle.capability_call.transitioned", target=requested.call_id
    )
    assert {item.decision for item in (*created_audits, *transition_audits)} == {
        "requested",
        "running",
        "succeeded",
    }


def test_capability_call_rejects_cross_scope_or_changed_identity(tmp_path) -> None:
    store = SQLiteControlPlane(tmp_path / "calls.db")
    _parent(store)
    service = CapabilityCallService(store)
    service.create_or_get(_spec())

    with pytest.raises(CapabilityCallConflictError, match="identity"):
        service.create_or_get(_spec(capability="task.delete"))
    with pytest.raises(CapabilityCallConflictError, match="inherit"):
        service.create_or_get(
            _spec(
                call_id="call:cross-scope",
                execution_key="execution-key-cross-scope",
                scope_id="scope-2",
            )
        )
