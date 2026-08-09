"""Safe LangChain callback projection for Deep Agents runtime lifecycle."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.callbacks import AsyncCallbackHandler

from ..logger import get_logger, log_event, safe_hash

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from langchain_core.outputs import LLMResult

    from .base import RunRequest

log = get_logger(__name__)


class AuditSink(Protocol):
    def append_audit(
        self,
        event_type: str,
        *,
        actor_id: str = "",
        scope_id: str = "",
        trace_id: str = "",
        target: str = "",
        decision: str = "",
        details: dict[str, Any] | None = None,
    ) -> str: ...


class DeepAgentTelemetry(AsyncCallbackHandler):
    """Observe native graph/model/tool callbacks without recording their content."""

    def __init__(self, request: RunRequest, audit_sink: AuditSink | None = None) -> None:
        self.request = request
        self.audit_sink = audit_sink
        self._started: dict[UUID, float] = {}
        self._tool_names: dict[UUID, str] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del messages, kwargs
        self._started[run_id] = time.monotonic()
        model = _component_name(serialized, "chat_model")
        self._event("model.started", "running", model=model)
        self._audit("model.call", model, "running", run_id, parent_run_id)

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        duration_ms = self._duration(run_id)
        usage = _usage(response)
        self._event(
            "model.completed",
            "succeeded",
            duration_ms=duration_ms,
            **usage,
        )
        self._audit(
            "model.call",
            "chat_model",
            "succeeded",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            **usage,
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        duration_ms = self._duration(run_id)
        self._event(
            "model.failed",
            "failed",
            level=logging.ERROR,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )
        self._audit(
            "model.call",
            "chat_model",
            "failed",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        name = _component_name(serialized, "tool")
        input_sha256 = safe_hash(inputs if inputs is not None else input_str)
        self._started[run_id] = time.monotonic()
        self._tool_names[run_id] = name
        self._event(
            "tool.started",
            "running",
            capability=name,
            input_sha256=input_sha256,
        )
        self._audit(
            "capability.call",
            name,
            "running",
            run_id,
            parent_run_id,
            input_sha256=input_sha256,
        )

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        name = self._tool_names.pop(run_id, "tool")
        duration_ms = self._duration(run_id)
        if isinstance(output, (str, bytes)):
            output_size = len(output)
        elif isinstance(output, list):
            output_size = sum(
                len(str(block.get("text", ""))) if isinstance(block, dict) else len(str(block))
                for block in output
            )
        else:
            output_size = len(str(output)) if output is not None else 0
        self._event(
            "tool.completed",
            "succeeded",
            capability=name,
            duration_ms=duration_ms,
            output_size=output_size,
        )
        self._audit(
            "capability.call",
            name,
            "succeeded",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            output_size=output_size,
        )

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        name = self._tool_names.pop(run_id, "tool")
        duration_ms = self._duration(run_id)
        self._event(
            "tool.failed",
            "failed",
            level=logging.ERROR,
            capability=name,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )
        self._audit(
            "capability.call",
            name,
            "failed",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
        )

    def _event(self, event: str, status: str, *, level: int = logging.INFO, **fields: Any) -> None:
        context = self.request.context
        log_event(
            log,
            event,
            level=level,
            status=status,
            trace_id=context.trace_id,
            task_id=self.request.metadata.get("task_id", ""),
            run_id=context.run_id,
            thread_id=context.run_id or context.trace_id,
            actor_id=context.actor_id,
            conversation_id=context.conversation_id,
            agent_ref=self.request.metadata.get("agent_ref", ""),
            skill_ref=self.request.metadata.get("skill_ref", ""),
            policy_ref=self.request.metadata.get("policy_ref", ""),
            **fields,
        )

    def _audit(
        self,
        event_type: str,
        target: str,
        decision: str,
        native_run_id: UUID,
        parent_run_id: UUID | None,
        **details: Any,
    ) -> None:
        if self.audit_sink is None:
            return
        context = self.request.context
        payload = {
            "schema_version": "1.0",
            "run_id": context.run_id,
            "runtime_call_id": str(native_run_id),
            "parent_runtime_call_id": str(parent_run_id or ""),
            "agent_ref": self.request.metadata.get("agent_ref", ""),
            "skill_ref": self.request.metadata.get("skill_ref", ""),
            **details,
        }
        try:
            self.audit_sink.append_audit(
                event_type,
                actor_id=context.actor_id,
                scope_id=context.scope,
                trace_id=context.trace_id,
                target=target,
                decision=decision,
                details=payload,
            )
        except Exception as error:
            self._event(
                "audit.write_failed",
                "degraded",
                level=logging.ERROR,
                error_code=type(error).__name__,
            )

    def _duration(self, run_id: UUID) -> int:
        started = self._started.pop(run_id, time.monotonic())
        return round((time.monotonic() - started) * 1000)


def _component_name(serialized: Mapping[str, Any], default: str) -> str:
    name = serialized.get("name")
    if isinstance(name, str) and name:
        return name
    path = serialized.get("id")
    if isinstance(path, (list, tuple)) and path:
        return str(path[-1])
    return default


def _usage(response: LLMResult) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0}
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "usage_metadata", None) or {}
            totals["input_tokens"] += int(metadata.get("input_tokens") or 0)
            totals["output_tokens"] += int(metadata.get("output_tokens") or 0)
    return totals
