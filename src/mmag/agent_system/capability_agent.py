"""Generic deterministic Agent backed by one declared capability."""

from __future__ import annotations

import json
import re
from typing import Any

from .core import AgentDescriptor, AgentOutput, AgentRequest

_URL = re.compile(r"https?://[^\s<>]+", re.I)


class CapabilityAgent:
    def __init__(
        self,
        descriptor: AgentDescriptor,
        capability,
        executor,
        *,
        source_argument: str | None = None,
        artifact_kind: str | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.capability = capability
        self.executor = executor
        self.source_argument = source_argument
        self.artifact_kind = artifact_kind

    async def run(self, request: AgentRequest) -> AgentOutput:
        arguments = dict(request.parameters)
        source = self._extract_source(request, arguments)
        result = await self.executor.execute(self.capability, arguments)
        payload = result.to_payload()
        structured = payload if isinstance(payload, dict) else {"result": payload}
        artifacts = self._artifacts(source, structured)
        return AgentOutput(
            json.dumps(structured, ensure_ascii=False, default=str),
            self.descriptor.name,
            artifacts,
            structured,
        )

    def _extract_source(self, request: AgentRequest, arguments: dict[str, Any]) -> str | None:
        if self.source_argument is None:
            return None
        existing = arguments.get(self.source_argument)
        if isinstance(existing, str) and existing:
            return existing
        match = _URL.search(request.prompt)
        if match is None:
            raise ValueError(
                f"Agent {self.descriptor.name!r} requires argument {self.source_argument!r}"
            )
        source = match.group(0).rstrip(".,;!?)")
        arguments[self.source_argument] = source
        return source

    def _artifacts(self, source: str | None, content: dict[str, Any]) -> tuple[dict, ...]:
        if self.artifact_kind is None:
            return ()
        artifact: dict[str, Any] = {"kind": self.artifact_kind, "content": content}
        if source is not None:
            artifact["source"] = source
        return (artifact,)
