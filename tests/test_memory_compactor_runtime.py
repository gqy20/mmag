from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.memory_compactor import MemoryCompactor
from mmag.runtimes import AgentResult, RunRequest, RuntimeUnavailableError


def _compactor(runtime) -> MemoryCompactor:
    config = SimpleNamespace(
        memory_summary_interval=100,
        memory_context_window=20,
        mm_installation_id="default",
        mm_tenant_id="default",
    )
    return MemoryCompactor(
        memory=MagicMock(),
        runtime=runtime,
        mm_client=MagicMock(),
        config=config,
    )


@pytest.mark.asyncio
async def test_summary_uses_provider_neutral_runtime_request():
    runtime = MagicMock()
    runtime.run = AsyncMock(return_value=AgentResult(text="summary", runtime="test"))
    compactor = _compactor(runtime)

    result = await compactor._summarize_message_batch(
        [{"username": "alice", "message": "ship it", "create_at": 0}],
        "channel-1",
    )

    assert result == "summary"
    request = runtime.run.await_args.args[0]
    assert isinstance(request, RunRequest)
    assert request.context.actor_id == "mmag:memory-compactor"
    assert request.context.conversation_id == "channel-1"
    assert request.context.scope == "mattermost:default:default:chn:channel-1"
    assert request.capabilities == ()
    assert request.max_rounds == 1
    assert request.max_tokens == 1024


@pytest.mark.asyncio
async def test_summary_runtime_failure_is_not_persistable_text():
    runtime = MagicMock()
    runtime.run = AsyncMock(side_effect=RuntimeUnavailableError("offline", runtime="test"))
    compactor = _compactor(runtime)

    result = await compactor._summarize_message_batch(
        [{"username": "alice", "message": "ship it"}],
        "channel-1",
    )

    assert result is None
