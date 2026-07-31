"""Adapter for the optional Claude Agent SDK backend."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .base import (
    AgentResult,
    AgentRuntimeError,
    RunRequest,
    RuntimeStatus,
    RuntimeTimeoutError,
    translate_runtime_error,
)

_EXHAUSTED_PREFIX = "⚠️ 处理超时"


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _remaining_seconds(request: RunRequest) -> float | None:
    deadline = request.context.deadline
    if deadline is None:
        return None
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
    return (deadline - now).total_seconds()


class _BackendRuntimeAdapter:
    runtime_name = "backend"

    def __init__(self, backend: Any, *, tool_registry: Any = None):
        self.backend = backend
        self.tool_registry = tool_registry

    async def run(self, request: RunRequest) -> AgentResult:
        remaining = _remaining_seconds(request)
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
        messages = _thaw(request.messages)
        capabilities = _thaw(request.capabilities)
        text = await self.backend.agent_loop(
            messages=messages,
            system=request.system_prompt,
            tools=capabilities,
            tool_registry=self.tool_registry,
            max_rounds=request.max_rounds,
            max_tokens=request.max_tokens,
        )

        if text.startswith(_EXHAUSTED_PREFIX):
            try:
                recovered = await self.backend.chat(
                    messages=messages,
                    system=request.system_prompt,
                    max_tokens=request.fallback_max_tokens,
                )
                if recovered and not recovered.startswith("(模型返回为空)"):
                    text = recovered
            except Exception:
                pass

        status = (
            RuntimeStatus.EXHAUSTED
            if text.startswith(_EXHAUSTED_PREFIX)
            else RuntimeStatus.COMPLETED
        )
        return AgentResult(text=text, runtime=self.runtime_name, status=status)


class ClaudeSDKRuntimeAdapter(_BackendRuntimeAdapter):
    runtime_name = "claude-sdk"
