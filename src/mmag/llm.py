"""Anthropic model client used by the LangGraph runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic

from .config import config
from .logger import get_logger, trace
from .model_artifacts import strip_model_artifacts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)


class LLMError(Exception):
    """Model-provider failure translated by the Runtime boundary."""


@dataclass
class ParsedResponse:
    """Structured text and tool calls from one model turn."""

    texts: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLM:
    """Thin asynchronous Anthropic client; graph orchestration lives elsewhere."""

    def __init__(self) -> None:
        kwargs: dict[str, Any] = {"api_key": config.anthropic_api_key}
        if config.anthropic_base_url:
            kwargs["base_url"] = config.anthropic_base_url
        self.client = AsyncAnthropic(**kwargs)
        self.model = config.anthropic_model
        self.call_count = 0
        log.info(
            "LLM 初始化完成 | 模型: %s | API: %s",
            self.model,
            config.anthropic_base_url or "官方",
        )

    async def chat(self, messages: list[dict], system: str = "", max_tokens: int = 1024) -> str:
        """Run a text-only model turn and remove provider compatibility artifacts."""
        started_at = time.monotonic()
        try:
            parsed = await self.complete(
                messages=messages,
                system=system,
                tools=[],
                max_tokens=max_tokens,
            )
            cleaned = strip_model_artifacts("\n".join(parsed.texts)).strip()
            log.debug(
                "%s LLM 单轮调用 (%.3fs, %d 字符输出)",
                trace.prefix(),
                time.monotonic() - started_at,
                len(cleaned),
            )
            return cleaned if cleaned else "(模型返回为空)"
        except LLMError:
            log.error(
                "%s LLM 调用失败 (%.3fs)",
                trace.prefix(),
                time.monotonic() - started_at,
                exc_info=True,
            )
            raise

    async def chat_stream(
        self,
        messages: list[dict],
        *,
        system: str = "",
        max_tokens: int = 1024,
        on_text: Callable[[str], Awaitable[None]],
    ) -> str:
        """Stream visible text deltas and return the canonical final text."""
        parsed = await self.complete_stream(
            messages=messages,
            system=system,
            tools=[],
            max_tokens=max_tokens,
            on_text=on_text,
        )
        cleaned = strip_model_artifacts("\n".join(parsed.texts)).strip()
        return cleaned if cleaned else "(模型返回为空)"

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ParsedResponse:
        """Return one structured Anthropic turn for LangGraph nodes."""
        kwargs = self._request_kwargs(messages, system, tools, max_tokens)
        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as error:
            raise LLMError(str(error)) from error
        return _parse_response(response)

    async def complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]],
    ) -> ParsedResponse:
        """Stream one Anthropic turn while retaining the final structured response."""
        kwargs = self._request_kwargs(messages, system, tools, max_tokens)
        try:
            async with self.client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    if text:
                        await on_text(text)
                response = await stream.get_final_message()
        except Exception as error:
            raise LLMError(str(error)) from error
        return _parse_response(response)

    def _request_kwargs(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> dict[str, Any]:
        self.call_count += 1
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "tools": tools,
        }
        if system:
            kwargs["system"] = system
        return kwargs


def _parse_response(response: Any) -> ParsedResponse:
    """Split model content into visible text and structured tool calls."""
    parsed = ParsedResponse()
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "thinking":
            continue
        if block_type == "text" and block.text.strip():
            parsed.texts.append(block.text)
        elif block_type == "tool_use":
            parsed.tool_calls.append(
                {"id": block.id, "name": block.name, "input": dict(block.input)}
            )
    return parsed
