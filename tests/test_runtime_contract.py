from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mmag.runtimes import (
    AgentResult,
    AgentRuntime,
    ClaudeSDKRuntimeAdapter,
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
from mmag.sdk_llm import SDKLLMError


def _request(**overrides) -> RunRequest:
    values = {
        "context": RunContext(
            trace_id="trace-1",
            actor_id="user-1",
            conversation_id="channel-1",
            scope="mattermost:team-1/channel-1",
        ),
        "messages": ({"role": "user", "content": "hello"},),
        "system_prompt": "system",
        "capabilities": ({"name": "get_posts"},),
        "max_rounds": 4,
        "max_tokens": 2048,
    }
    values.update(overrides)
    return RunRequest(**values)


def _adapter(adapter_type):
    backend = SimpleNamespace(
        run_agent=AsyncMock(return_value="done"),
        chat=AsyncMock(return_value="recovered"),
    )
    return adapter_type(backend), backend


def test_runtime_contract_values_are_immutable():
    request = _request()
    result = AgentResult(text="done", runtime="test")

    with pytest.raises(FrozenInstanceError):
        request.max_rounds = 9
    with pytest.raises(FrozenInstanceError):
        result.text = "changed"


def test_agent_result_has_structured_defaults():
    result = AgentResult(text="done", runtime="test")

    assert result.status is RuntimeStatus.COMPLETED
    assert result.artifacts == ()
    assert result.capability_calls == ()
    assert result.usage == TokenUsage()


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


@pytest.mark.asyncio
async def test_claude_adapter_returns_structured_result():
    adapter, backend = _adapter(ClaudeSDKRuntimeAdapter)

    result = await adapter.run(_request())

    assert isinstance(adapter, AgentRuntime)
    assert result == AgentResult(text="done", runtime="claude-sdk")
    backend.run_agent.assert_awaited_once_with(
        messages=[{"role": "user", "content": "hello"}],
        system="system",
    )


@pytest.mark.asyncio
async def test_claude_adapter_owns_the_no_tools_fallback():
    adapter, backend = _adapter(ClaudeSDKRuntimeAdapter)
    backend.run_agent.return_value = "⚠️ 处理超时，请重试"

    result = await adapter.run(_request())

    assert result.text == "recovered"
    backend.chat.assert_awaited_once_with(
        messages=[{"role": "user", "content": "hello"}],
        system="system",
        max_tokens=1024,
    )


@pytest.mark.asyncio
async def test_claude_adapter_translates_backend_errors():
    adapter, backend = _adapter(ClaudeSDKRuntimeAdapter)
    error = SDKLLMError("request failed")
    error.__cause__ = TimeoutError("deadline exceeded")
    backend.run_agent.side_effect = error

    with pytest.raises(RuntimeTimeoutError) as raised:
        await adapter.run(_request())

    assert raised.value.runtime == "claude-sdk"
    assert raised.value.__cause__ is error


@pytest.mark.asyncio
async def test_expired_deadline_fails_before_calling_backend():
    adapter, backend = _adapter(ClaudeSDKRuntimeAdapter)
    context = _request().context
    expired = RunContext(
        trace_id=context.trace_id,
        actor_id=context.actor_id,
        conversation_id=context.conversation_id,
        scope=context.scope,
        deadline=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(RuntimeTimeoutError):
        await adapter.run(_request(context=expired))

    backend.run_agent.assert_not_awaited()
