"""Adapter for the optional Claude Agent SDK backend."""

from __future__ import annotations

import asyncio
from typing import Any

from .base import (
    AgentResult,
    AgentRuntimeError,
    RunRequest,
    RuntimeStatus,
    RuntimeTimeoutError,
    recover_exhausted,
    remaining_seconds,
    thaw,
    translate_runtime_error,
)

_EXHAUSTED_PREFIX = "⚠️ 处理超时"


class ClaudeSDKRuntimeAdapter:
    """Translate the optional Claude SDK backend to the Runtime contract."""

    runtime_name = "claude-sdk"

    def __init__(self, backend: Any, *, tool_registry: Any = None) -> None:
        self.backend = backend
        self.tool_registry = tool_registry

    async def run(self, request: RunRequest) -> AgentResult:
        remaining = remaining_seconds(request)
        if remaining is not None and remaining <= 0:
            raise RuntimeTimeoutError("runtime deadline exceeded", runtime=self.runtime_name)
        try:
            if remaining is None:
                return await self._execute(request)
            async with asyncio.timeout(remaining):
                return await self._execute(request)
        except AgentRuntimeError:
            raise
        except Exception as error:
            translated = translate_runtime_error(error, runtime=self.runtime_name)
            raise translated from error

    async def _execute(self, request: RunRequest) -> AgentResult:
        messages = thaw(request.messages)
        capabilities = thaw(request.capabilities)
        text = await self.backend.agent_loop(
            messages=messages,
            system=request.system_prompt,
            tools=capabilities,
            tool_registry=self.tool_registry,
            max_rounds=request.max_rounds,
            max_tokens=request.max_tokens,
        )
        text = await recover_exhausted(self.backend, request, messages, text)
        status = (
            RuntimeStatus.EXHAUSTED
            if text.startswith(_EXHAUSTED_PREFIX)
            else RuntimeStatus.COMPLETED
        )
        return AgentResult(text=text, runtime=self.runtime_name, status=status)
