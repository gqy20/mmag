from unittest.mock import AsyncMock, MagicMock

import pytest

from mmag.llm import LLM, LLMError
from mmag.model_artifacts import strip_model_artifacts


class FakeBlock:
    def __init__(self, block_type: str, **values):
        self.type = block_type
        for key, value in values.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, blocks: list[FakeBlock]):
        self.content = blocks


def _llm() -> LLM:
    llm = LLM.__new__(LLM)
    llm.client = MagicMock()
    llm.client.messages = MagicMock()
    llm.client.messages.create = AsyncMock()
    llm.model = "test-model"
    llm.call_count = 0
    return llm


@pytest.mark.asyncio
async def test_complete_returns_structured_text_and_tool_calls():
    llm = _llm()
    llm.client.messages.create.return_value = FakeResponse(
        [
            FakeBlock("thinking", thinking="private"),
            FakeBlock("text", text="I will check"),
            FakeBlock("tool_use", id="call-1", name="search", input={"q": "x"}),
        ]
    )

    result = await llm.complete(
        messages=[{"role": "user", "content": "search"}],
        system="system",
        tools=[{"name": "search"}],
        max_tokens=2048,
    )

    assert result.texts == ["I will check"]
    assert result.tool_calls == [{"id": "call-1", "name": "search", "input": {"q": "x"}}]
    assert llm.call_count == 1
    assert llm.client.messages.create.await_args.kwargs["system"] == "system"


@pytest.mark.asyncio
async def test_chat_strips_model_artifacts_and_empty_text_has_stable_fallback():
    llm = _llm()
    llm.client.messages.create.side_effect = [
        FakeResponse(
            [FakeBlock("text", text="<think>private</think><tool_call>x</tool_call>answer")]
        ),
        FakeResponse([FakeBlock("text", text=" ")]),
    ]

    assert await llm.chat([{"role": "user", "content": "hi"}]) == "answer"
    assert await llm.chat([{"role": "user", "content": "hi"}]) == "(模型返回为空)"


@pytest.mark.asyncio
async def test_provider_error_is_wrapped():
    llm = _llm()
    llm.client.messages.create.side_effect = RuntimeError("network down")

    with pytest.raises(LLMError, match="network down"):
        await llm.complete(messages=[], system="", tools=[], max_tokens=10)


def test_artifact_filter_preserves_normal_text():
    assert strip_model_artifacts("normal response") == "normal response"
