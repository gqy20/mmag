from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mmag.llm import LLMError
from mmag.runtimes import (
    AgentResult,
    AgentRuntime,
    ClaudeSDKRuntimeAdapter,
    LegacyRuntimeAdapter,
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
        agent_loop=AsyncMock(return_value="done"),
        chat=AsyncMock(return_value="recovered"),
    )
    registry = object()
    return adapter_type(backend, tool_registry=registry), backend, registry


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


@pytest.mark.parametrize(
    ("adapter_type", "runtime_name"),
    [
        (LegacyRuntimeAdapter, "langgraph"),
        (ClaudeSDKRuntimeAdapter, "claude-sdk"),
    ],
)
@pytest.mark.asyncio
async def test_adapters_return_the_same_structured_result(adapter_type, runtime_name):
    adapter, backend, registry = _adapter(adapter_type)

    result = await adapter.run(_request())

    assert isinstance(adapter, AgentRuntime)
    assert result == AgentResult(text="done", runtime=runtime_name)
    backend.agent_loop.assert_awaited_once_with(
        messages=[{"role": "user", "content": "hello"}],
        system="system",
        tools=[{"name": "get_posts"}],
        tool_registry=registry,
        max_rounds=4,
        max_tokens=2048,
    )


@pytest.mark.parametrize("adapter_type", [LegacyRuntimeAdapter, ClaudeSDKRuntimeAdapter])
@pytest.mark.asyncio
async def test_adapters_own_the_no_tools_fallback(adapter_type):
    adapter, backend, _registry = _adapter(adapter_type)
    backend.agent_loop.return_value = "⚠️ 处理超时，请重试"

    result = await adapter.run(_request())

    assert result.text == "recovered"
    backend.chat.assert_awaited_once_with(
        messages=[{"role": "user", "content": "hello"}],
        system="system",
        max_tokens=2048,
    )


@pytest.mark.parametrize(
    ("adapter_type", "backend_error"),
    [(LegacyRuntimeAdapter, LLMError), (ClaudeSDKRuntimeAdapter, SDKLLMError)],
)
@pytest.mark.asyncio
async def test_adapters_translate_backend_errors(adapter_type, backend_error):
    adapter, backend, _registry = _adapter(adapter_type)
    error = backend_error("request failed")
    error.__cause__ = TimeoutError("deadline exceeded")
    backend.agent_loop.side_effect = error

    with pytest.raises(RuntimeTimeoutError) as raised:
        await adapter.run(_request())

    assert raised.value.runtime in {"langgraph", "claude-sdk"}
    assert raised.value.__cause__ is error


@pytest.mark.asyncio
async def test_expired_deadline_fails_before_calling_backend():
    adapter, backend, _registry = _adapter(LegacyRuntimeAdapter)
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

    backend.agent_loop.assert_not_awaited()
