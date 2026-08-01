from dataclasses import FrozenInstanceError

import pytest

from mmag.runtimes import (
    AgentResult,
    RunContext,
    RunRequest,
    RuntimeInternalError,
    RuntimeRateLimitError,
    RuntimeRejectedError,
    RuntimeStatus,
    RuntimeTimeoutError,
    RuntimeUnavailableError,
    TokenUsage,
    translate_runtime_error,
)


def _request() -> RunRequest:
    return RunRequest(
        context=RunContext("trace-1", "user-1", "channel-1", "scope-1"),
        messages=({"role": "user", "content": "hello"},),
        response_schema={"type": "object"},
        skill_files={"/skills/demo/SKILL.md": {"content": "demo", "encoding": "utf-8"}},
    )


def test_runtime_contract_values_and_nested_inputs_are_immutable():
    request = _request()
    result = AgentResult(text="done", runtime="test")

    with pytest.raises(FrozenInstanceError):
        request.max_rounds = 9
    with pytest.raises(TypeError):
        request.skill_files["other"] = {}
    with pytest.raises(FrozenInstanceError):
        result.text = "changed"


def test_agent_result_has_structured_defaults():
    result = AgentResult(text="done", runtime="test")

    assert result.status is RuntimeStatus.COMPLETED
    assert result.artifacts == ()
    assert result.capability_calls == ()
    assert result.usage == TokenUsage()
    assert result.output is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("deadline"), RuntimeTimeoutError),
        (RuntimeError("rate limit exceeded"), RuntimeRateLimitError),
        (RuntimeError("content policy rejected"), RuntimeRejectedError),
        (ConnectionError("connection lost"), RuntimeUnavailableError),
        (RuntimeError("unexpected"), RuntimeInternalError),
    ],
)
def test_runtime_errors_are_classified(error, expected):
    assert isinstance(translate_runtime_error(error, runtime="test"), expected)
