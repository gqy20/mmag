import pytest

from mmag.control_plane import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalService,
    Artifact,
    EntityType,
    InvalidTransitionError,
    LifecycleService,
    OutboundMessage,
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
    created_audit = store.list_audits(
        event_type="lifecycle.agent_run.created", target="run-1"
    )
    transition_audit = store.list_audits(
        event_type="lifecycle.agent_run.transitioned", target="run-1"
    )
    assert len(created_audit) == len(transition_audit) == 1
    assert transition_audit[0].details == {
        "schema_version": "1.0",
        "entity_type": "agent_run",
        "entity_id": "run-1",
        "from_state": "queued",
        "to_state": "running",
        "version": 1,
        "command_id": "start-1",
        "recovery": False,
        "run_id": "run-1",
    }

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


def test_lifecycle_transition_and_audit_roll_back_together(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    lifecycle.create(EntityType.AGENT_RUN, "run-atomic", scope_id="scope-1")
    store._connection.execute(  # noqa: SLF001 - transaction fault injection
        """CREATE TRIGGER reject_lifecycle_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.event_type='lifecycle.agent_run.transitioned'
        BEGIN SELECT RAISE(ABORT, 'audit write failed'); END"""
    )

    with pytest.raises(Exception, match="audit write failed"):
        lifecycle.transition(
            EntityType.AGENT_RUN,
            "run-atomic",
            "running",
            command_id="start-atomic",
            expected_version=0,
        )

    entity = store.get_lifecycle_entity(EntityType.AGENT_RUN, "run-atomic")
    assert entity.state == "queued"
    assert entity.version == 0
    assert store.list_transitions(EntityType.AGENT_RUN, "run-atomic") == []
    assert store.list_audits(
        event_type="lifecycle.agent_run.transitioned", target="run-atomic"
    ) == []
    store.close()


def test_delivery_projection_lifecycle_and_audit_roll_back_together(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    delivery_id = store.enqueue_delivery(
        OutboundMessage(
            conversation_id="conversation-1",
            text="private result",
            idempotency_key="atomic-delivery-1",
            actor_id="user-1",
            scope_id="scope-1",
        )
    )
    store._connection.execute(  # noqa: SLF001 - transaction fault injection
        """CREATE TRIGGER reject_delivery_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.event_type='lifecycle.delivery.transitioned'
        BEGIN SELECT RAISE(ABORT, 'delivery audit write failed'); END"""
    )

    with pytest.raises(Exception, match="delivery audit write failed"):
        lifecycle.transition(
            EntityType.DELIVERY,
            delivery_id,
            "sending",
            command_id="send-atomic-delivery",
            delivery_attempts="increment",
        )

    delivery = store.get_delivery(delivery_id)
    entity = store.get_lifecycle_entity(EntityType.DELIVERY, delivery_id)
    assert (delivery.status, delivery.attempts) == ("pending", 0)
    assert (entity.state, entity.version) == ("pending", 0)
    assert store.list_transitions(EntityType.DELIVERY, delivery_id) == []
    store.close()


def test_approval_projection_lifecycle_and_audit_roll_back_together(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    approvals = ApprovalService(store, lifecycle)
    request = approvals.request("send_file", {}, requested_by="user-1")
    store._connection.execute(  # noqa: SLF001 - transaction fault injection
        """CREATE TRIGGER reject_approval_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.event_type='lifecycle.approval_request.transitioned'
        BEGIN SELECT RAISE(ABORT, 'approval audit write failed'); END"""
    )

    with pytest.raises(Exception, match="approval audit write failed"):
        approvals.decide(request.id, approved=True, actor_id="owner-1")

    assert store.get_approval_request(request.id).state.value == "pending"
    assert store.list_transitions(EntityType.APPROVAL_REQUEST, request.id) == []
    store._connection.execute("DROP TRIGGER reject_approval_audit")  # noqa: SLF001
    assert approvals.decide(request.id, approved=True, actor_id="owner-1").state.value == (
        "approved"
    )
    store.close()


def test_reconcile_recovers_interrupted_states(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    lifecycle.create(EntityType.AGENT_RUN, "run-1")
    lifecycle.transition(EntityType.AGENT_RUN, "run-1", "running", command_id="start")
    delivery_id = store.enqueue_delivery(
        OutboundMessage(
            conversation_id="conversation-1",
            text="result",
            idempotency_key="reconcile-delivery-1",
            actor_id="user-1",
            scope_id="scope-1",
        )
    )
    lifecycle.transition(
        EntityType.DELIVERY,
        delivery_id,
        "sending",
        command_id="send",
        delivery_attempts="increment",
    )

    recovered = lifecycle.reconcile()

    assert {(item.entity_type, item.state) for item in recovered} == {
        (EntityType.AGENT_RUN, "queued"),
        (EntityType.DELIVERY, "pending"),
    }
    assert store.get_delivery(delivery_id).status == "pending"
    store.close()


def test_approval_persists_arguments_and_resume_token(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    lifecycle = LifecycleService(store)
    approvals = ApprovalService(store, lifecycle)
    request = approvals.request(
        "send_file",
        {"path": "report.pdf"},
        requested_by="user-1",
        scope_id="project:p1",
        trace_id="trace-approval-1",
    )
    decided = approvals.decide(request.id, approved=True, actor_id="owner-1")
    assert decided.state.value == "approved"
    assert decided.arguments == {"path": "report.pdf"}
    assert decided.resume_token == request.resume_token
    created_audit = store.list_audits(
        event_type="lifecycle.approval_request.created", target=request.id
    )[0]
    decided_audit = store.list_audits(
        event_type="lifecycle.approval_request.transitioned", target=request.id
    )[0]
    assert created_audit.trace_id == decided_audit.trace_id == "trace-approval-1"
    assert created_audit.actor_id == "user-1"
    assert decided_audit.actor_id == "owner-1"
    assert "resume_token" not in created_audit.details
    assert "path" not in str(created_audit.details)
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


def test_artifact_record_and_safe_audit_are_atomic(tmp_path):
    store = SQLiteControlPlane(tmp_path / "control.db")
    artifact = Artifact(
        "artifact-1",
        "run-1",
        "scope-1",
        "research_report",
        "ab/artifact-1/report.md",
        {
            "schema_version": "1.0",
            "sha256": "a" * 64,
            "size_bytes": 42,
            "filename": "sensitive-report.md",
        },
    )

    store.create_artifact(artifact)

    audit = store.list_audits(event_type="artifact.created", target=artifact.id)[0]
    assert audit.scope_id == "scope-1"
    assert audit.details["kind"] == "research_report"
    assert "filename" not in audit.details
    assert store.metrics.value(
        "mmag_artifacts_total", status="created", event_type="research_report"
    ) == 1
    store.close()
