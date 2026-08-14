from mmag.evaluation import (
    ControlPlaneObservation,
    DeterministicEvaluator,
    EvaluationObservation,
    EvaluationScenario,
    TaskObservation,
)


def test_security_assertion_rejects_unauthorized_approval():
    scenario = EvaluationScenario(
        id="approval-security",
        version="1.0.0",
        actor="requester",
        message="approve",
        expected={
            "response": {"kind": "error", "terminal_status": "failed"},
            "approval": {"required": True, "authorized": False},
        },
    )
    observation = EvaluationObservation(
        root_post_id="post-1",
        run_id="mattermost:post-1",
        response_text="当前审批无法处理。",
        response_kind="error",
        terminal_status="failed",
        approval_seen=True,
        approval_decision_denied=True,
    )

    assertions = DeterministicEvaluator().evaluate(scenario, observation)

    assert all(assertion.passed for assertion in assertions)
    assert any(assertion.severity == "security" for assertion in assertions)


def test_raw_json_is_reported_as_presentation_failure():
    scenario = EvaluationScenario(
        id="raw-json",
        version="1.0.0",
        actor="requester",
        message="report",
        expected={"response": {"raw_json_forbidden": True}},
    )

    assertions = DeterministicEvaluator().evaluate(
        scenario,
        EvaluationObservation(
            root_post_id="post-1",
            run_id="mattermost:post-1",
            response_text='{"summary":"raw"}',
        ),
    )

    assert not next(item for item in assertions if item.name == "raw_json_not_exposed").passed


def test_actual_agent_name_is_asserted_from_control_plane():
    scenario = EvaluationScenario(
        id="agent-route",
        version="1.0.0",
        actor="requester",
        message="@bot chat",
        expected={"control_plane": {"agent_name": "mmchat"}},
    )
    observation = EvaluationObservation(
        root_post_id="post-1",
        run_id="mattermost:post-1",
        control_plane=ControlPlaneObservation(agent_name="project"),
    )

    assertion = next(
        item
        for item in DeterministicEvaluator().evaluate(scenario, observation)
        if item.name == "agent_name"
    )
    assert not assertion.passed
    assert assertion.expected == "mmchat"
    assert assertion.actual == "project"


def test_task_and_capability_assertions_use_control_plane_state():
    scenario = EvaluationScenario(
        id="project-task",
        version="1.0.0",
        actor="requester",
        message="create task",
        expected={
            "capabilities": {"contains_all": ["create_task"]},
            "tasks": {
                "minimum_created": 1,
                "title_contains": "MMAG-E2E-",
                "requester_is_creator": True,
                "current_channel": True,
                "execution_key_required": True,
            },
        },
    )
    observation = EvaluationObservation(
        root_post_id="post-1",
        run_id="mattermost:post-1",
        control_plane=ControlPlaneObservation(capability_names=("create_task",)),
        created_tasks=(
            TaskObservation(
                "task-1",
                "MMAG-E2E-one",
                "mattermost:i:t:chn:c",
                True,
                True,
                True,
            ),
        ),
    )

    assert all(item.passed for item in DeterministicEvaluator().evaluate(scenario, observation))
