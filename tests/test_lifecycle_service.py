import pytest

from mmag.control_plane import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalService,
    EntityType,
    InvalidTransitionError,
    LifecycleService,
    SQLiteControlPlane,
    VersionConflictError,
)


def test_lifecycle_is_versioned_idempotent_and_append_only(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    entity = lifecycle.create(EntityType.AGENT_RUN, "run-1", scope_id="scope-1")
    assert entity.state == "queued"
    running = lifecycle.transition(
        EntityType.AGENT_RUN,
        "run-1",
        "running",
        command_id="start-1",
        expected_version=0,
    )
    duplicate = lifecycle.transition(
        EntityType.AGENT_RUN,
        "run-1",
        "running",
        command_id="start-1",
        expected_version=0,
    )
    assert running == duplicate
    assert len(store.list_transitions(EntityType.AGENT_RUN, "run-1")) == 1

    with pytest.raises(VersionConflictError):
        lifecycle.transition(
            EntityType.AGENT_RUN,
            "run-1",
            "failed",
            command_id="fail-stale",
            expected_version=0,
        )
    with pytest.raises(InvalidTransitionError):
        lifecycle.transition(
            EntityType.AGENT_RUN,
            "run-1",
            "queued",
            command_id="backwards",
            expected_version=1,
        )
    store.close()


def test_reconcile_recovers_interrupted_states(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    lifecycle.create(EntityType.AGENT_RUN, "run-1")
    lifecycle.transition(EntityType.AGENT_RUN, "run-1", "running", command_id="start")
    lifecycle.create(EntityType.DELIVERY, "delivery-1")
    lifecycle.transition(EntityType.DELIVERY, "delivery-1", "sending", command_id="send")

    recovered = lifecycle.reconcile()

    assert {(item.entity_type, item.state) for item in recovered} == {
        (EntityType.AGENT_RUN, "queued"),
        (EntityType.DELIVERY, "pending"),
    }
    store.close()


def test_approval_persists_arguments_and_resume_token(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    approvals = ApprovalService(store, lifecycle)
    request = approvals.request(
        "send_file", {"path": "report.pdf"}, requested_by="user-1", scope_id="project:p1"
    )
    decided = approvals.decide(request.id, approved=True, actor_id="owner-1")
    assert decided.state.value == "approved"
    assert decided.arguments == {"path": "report.pdf"}
    assert decided.resume_token == request.resume_token
    store.close()


def test_expired_approval_cannot_be_approved_or_decided_twice(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    approvals = ApprovalService(store, lifecycle)
    expired = approvals.request(
        "send_file",
        {"path": "report.pdf"},
        requested_by="user-1",
        ttl_seconds=-1,
    )

    with pytest.raises(ApprovalExpiredError):
        approvals.decide(expired.id, approved=True, actor_id="user-1")
    assert store.get_approval_request(expired.id).state.value == "expired"

    decided = approvals.request("send_file", {}, requested_by="user-1")
    approvals.decide(decided.id, approved=False, actor_id="user-1")
    with pytest.raises(ApprovalAlreadyDecidedError):
        approvals.decide(decided.id, approved=True, actor_id="user-1")
    store.close()
