"""
LLM.agent_loop (LangGraph) 单元测试

覆盖:
  - 无工具 → 降级 chat()
  - 单轮纯文本回复 (无 tool_call)
  - 多轮工具调用 (1 轮工具 + 1 轮文本)
  - 末轮强制禁用 tools
  - 空文本兜底
  - LLM 调用异常 → LLMError
  - 国产模型痕迹过滤
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.llm import LLM, LLMError  # noqa: E402

# ============================================================
# Fakes
# ============================================================


class FakeBlock:
    """模拟 Anthropic response content block"""

    def __init__(self, block_type: str, **kwargs):
        self.type = block_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResponse:
    """模拟 Anthropic messages.create 返回值"""

    def __init__(self, blocks: list[FakeBlock]):
        self.content = blocks


def _text_block(text: str) -> FakeBlock:
    return FakeBlock("text", text=text)


def _tool_use_block(tool_id: str, name: str, tool_input: dict) -> FakeBlock:
    return FakeBlock("tool_use", id=tool_id, name=name, input=tool_input)


def _make_llm() -> LLM:
    """构造 LLM 实例, mock 掉 AsyncAnthropic"""
    llm = LLM.__new__(LLM)
    llm.client = MagicMock()
    llm.client.messages = MagicMock()
    llm.client.messages.create = AsyncMock()
    llm.model = "test-model"
    llm.call_count = 0
    return llm


def _make_mock_registry():
    """构造 mock ToolRegistry"""
    reg = MagicMock()
    reg.execute = AsyncMock(return_value='{"result": "ok"}')
    return reg


# ============================================================
# Tests
# ============================================================


class TestAgentLoopNoTools:
    @pytest.mark.asyncio
    async def test_degrades_to_chat(self):
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([_text_block("hello")])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "hi"}],
            system="test",
            tools=None,
            tool_registry=None,
        )
        assert result == "hello"
        assert llm.call_count == 1


class TestAgentLoopSingleTurn:
    @pytest.mark.asyncio
    async def test_text_reply_no_tool_calls(self):
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([_text_block("你好")])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "打招呼"}],
            system="sys",
            tools=[{"name": "dummy", "description": "d", "input_schema": {}}],
            tool_registry=_make_mock_registry(),
            max_rounds=5,
        )
        assert result == "你好"

    @pytest.mark.asyncio
    async def test_empty_text_fallback(self):
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([_text_block("   ")])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[{"name": "d", "description": "", "input_schema": {}}],
            tool_registry=_make_mock_registry(),
            max_rounds=3,
        )
        assert "处理超时" in result


class TestAgentLoopMultiTurn:
    @pytest.mark.asyncio
    async def test_one_tool_then_text(self):
        """
        Round 1: LLM 返回 tool_use → 执行工具
        Round 2: LLM 返回纯文本 → 结束
        """
        llm = _make_llm()
        reg = _make_mock_registry()

        # 两次调 create: 第一次返回 tool_use, 第二次返回 text
        llm.client.messages.create.side_effect = [
            FakeResponse([_tool_use_block("t1", "get_posts", {"channel_id": "ch1"})]),
            FakeResponse([_text_block("找到 3 条消息")]),
        ]

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "查看频道消息"}],
            system="sys",
            tools=[{"name": "get_posts", "description": "", "input_schema": {}}],
            tool_registry=reg,
            max_rounds=5,
        )
        assert result == "找到 3 条消息"
        assert llm.call_count == 2
        reg.execute.assert_called_once_with("get_posts", {"channel_id": "ch1"})

    @pytest.mark.asyncio
    async def test_multiline_tool_calls_text_preserved(self):
        """LLM 同时返回文本和工具调用 → 文本应保留在 assistant 消息里"""
        llm = _make_llm()
        reg = _make_mock_registry()
        llm.client.messages.create.side_effect = [
            FakeResponse([
                _text_block("让我查一下"),
                _tool_use_block("t1", "get_posts", {"channel_id": "ch1"}),
            ]),
            FakeResponse([_text_block("结果是空的")]),
        ]

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "查消息"}],
            system="",
            tools=[{"name": "get_posts", "description": "", "input_schema": {}}],
            tool_registry=reg,
            max_rounds=5,
        )
        assert result == "结果是空的"

    @pytest.mark.asyncio
    async def test_multiple_tools_in_one_turn(self):
        """一轮返回多个 tool_call → 全部执行"""
        llm = _make_llm()
        reg = _make_mock_registry()
        llm.client.messages.create.side_effect = [
            FakeResponse([
                _tool_use_block("t1", "get_posts", {"channel_id": "ch1"}),
                _tool_use_block("t2", "search_messages", {"query": "test"}),
            ]),
            FakeResponse([_text_block("查完了")]),
        ]

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "查两个东西"}],
            system="",
            tools=[
                {"name": "get_posts", "description": "", "input_schema": {}},
                {"name": "search_messages", "description": "", "input_schema": {}},
            ],
            tool_registry=reg,
            max_rounds=5,
        )
        assert result == "查完了"
        assert reg.execute.call_count == 2


class TestAgentLoopFinalRound:
    @pytest.mark.asyncio
    async def test_final_round_forces_text(self):
        """
        max_rounds=1 → 首轮即末轮 → tools 被禁用 → LLM 必须出文本
        """
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([_text_block("快速回复")])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[{"name": "d", "description": "", "input_schema": {}}],
            tool_registry=_make_mock_registry(),
            max_rounds=1,
        )
        assert result == "快速回复"
        # 验证末轮调用时 tools=[]
        call_kwargs = llm.client.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == []

    @pytest.mark.asyncio
    async def test_final_round_empty_text_fallback(self):
        """末轮 + 空文本 → 兜底"""
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([_text_block("")])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[{"name": "d", "description": "", "input_schema": {}}],
            tool_registry=_make_mock_registry(),
            max_rounds=1,
        )
        assert "处理超时" in result


class TestAgentLoopError:
    @pytest.mark.asyncio
    async def test_llm_error_raises(self):
        llm = _make_llm()
        llm.client.messages.create.side_effect = RuntimeError("network down")

        with pytest.raises(LLMError, match="Round 1"):
            await llm.agent_loop(
                messages=[{"role": "user", "content": "hi"}],
                system="",
                tools=[{"name": "d", "description": "", "input_schema": {}}],
                tool_registry=_make_mock_registry(),
                max_rounds=3,
            )


class TestAgentLoopArtifactStripping:
    @pytest.mark.asyncio
    async def test_strips_thinking_tags(self):
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([
            _text_block("<think>内心独白</think>实际回复"),
        ])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[{"name": "d", "description": "", "input_schema": {}}],
            tool_registry=_make_mock_registry(),
            max_rounds=3,
        )
        assert result == "实际回复"

    @pytest.mark.asyncio
    async def test_strips_invoke_xml(self):
        llm = _make_llm()
        llm.client.messages.create.return_value = FakeResponse([
            _text_block('<tool_call>{"name":"test","arguments":{}}</tool_call>回复内容'),
        ])

        result = await llm.agent_loop(
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[{"name": "d", "description": "", "input_schema": {}}],
            tool_registry=_make_mock_registry(),
            max_rounds=3,
        )
        assert result == "回复内容"
