"""Safe LangChain callback projection for Deep Agents runtime lifecycle."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.callbacks import GraphCallbackHandler, GraphInterruptEvent, GraphResumeEvent

from ..logger import get_logger, log_context, log_event, safe_hash

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
        self.workflow_id = log_context.get(
            "workflow_id", request.context.run_id or request.context.trace_id
        )
        self.parent_run_id = log_context.get("parent_run_id")
        self._started: dict[UUID, float] = {}
        self._tool_names: dict[UUID, str] = {}
        self._native_fields: dict[UUID, dict[str, Any]] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._started[run_id] = time.monotonic()
        native_fields = _native_metadata(metadata)
        self._native_fields[run_id] = native_fields
        model = _component_name(serialized, "chat_model")
        prior_tool_calls = 0
        for msg_list in messages:
            for msg in msg_list:
                prior_tool_calls = len(getattr(msg, "tool_calls", []) or [])
                break
            break
        msg_count = sum(len(ml) for ml in messages)
        self._event(
            "runtime.model.started",
            "running",
            span_id=str(run_id),
            parent_span_id=str(parent_run_id or ""),
            model=model,
            message_count=msg_count,
            prior_tool_calls=prior_tool_calls,
            **native_fields,
        )
        self._audit(
            "model.call",
            model,
            "running",
            run_id,
            parent_run_id,
            **native_fields,
        )

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
        native_fields = self._native_fields.pop(run_id, {})
        usage = _usage(response)
        # Diagnose whether the model returned tool calls
        stop_reasons: list[str] = []
        response_tool_calls: list[str] = []
        content_block_types: list[str] = []
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                stop = getattr(message, "response_metadata", None)
                if isinstance(stop, dict):
                    reason = stop.get("stop_reason") or stop.get("stop") or ""
                    if reason:
                        stop_reasons.append(str(reason))
                tc = getattr(message, "tool_calls", None) or []
                response_tool_calls.extend(
                    str(tc_item.get("name", "?")) if isinstance(tc_item, dict) else "?"
                    for tc_item in tc
                )
                content = getattr(message, "content", None)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            bt = str(block.get("type", "?"))
                            if bt not in content_block_types:
                                content_block_types.append(bt)
                elif isinstance(content, str) and content and "text" not in content_block_types:
                    content_block_types.append("text")
        self._event(
            "runtime.model.completed",
            "succeeded",
            span_id=str(run_id),
            parent_span_id=str(parent_run_id or ""),
            duration_ms=duration_ms,
            stop_reason=",".join(stop_reasons) if stop_reasons else "",
            response_tool_calls=response_tool_calls or None,
            content_block_types=content_block_types or None,
            **native_fields,
            **usage,
        )
        self._audit(
            "model.call",
            "chat_model",
            "succeeded",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            **native_fields,
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
        native_fields = self._native_fields.pop(run_id, {})
        self._event(
            "runtime.model.failed",
            "failed",
            level=logging.ERROR,
            span_id=str(run_id),
            parent_span_id=str(parent_run_id or ""),
            duration_ms=duration_ms,
            error_code=type(error).__name__,
            **native_fields,
        )
        self._audit(
            "model.call",
            "chat_model",
            "failed",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
            **native_fields,
        )

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        name = _component_name(serialized, "tool")
        input_sha256 = safe_hash(inputs if inputs is not None else input_str)
        native_fields = _native_metadata(metadata)
        self._started[run_id] = time.monotonic()
        self._tool_names[run_id] = name
        self._native_fields[run_id] = native_fields
        self._event(
            "runtime.tool.started",
            "running",
            span_id=str(run_id),
            parent_span_id=str(parent_run_id or ""),
            capability=name,
            input_sha256=input_sha256,
            **native_fields,
        )
        self._audit(
            "runtime.tool.call",
            name,
            "running",
            run_id,
            parent_run_id,
            input_sha256=input_sha256,
            **native_fields,
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
        native_fields = self._native_fields.pop(run_id, {})
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
            "runtime.tool.completed",
            "succeeded",
            span_id=str(run_id),
            parent_span_id=str(parent_run_id or ""),
            capability=name,
            duration_ms=duration_ms,
            output_size=output_size,
            **native_fields,
        )
        self._audit(
            "runtime.tool.call",
            name,
            "succeeded",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            output_size=output_size,
            **native_fields,
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
        native_fields = self._native_fields.pop(run_id, {})
        self._event(
            "runtime.tool.failed",
            "failed",
            level=logging.ERROR,
            span_id=str(run_id),
            parent_span_id=str(parent_run_id or ""),
            capability=name,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
            **native_fields,
        )
        self._audit(
            "runtime.tool.call",
            name,
            "failed",
            run_id,
            parent_run_id,
            duration_ms=duration_ms,
            error_code=type(error).__name__,
            **native_fields,
        )

    def _event(self, event: str, status: str, *, level: int = logging.INFO, **fields: Any) -> None:
        context = self.request.context
        log_event(
            log,
            event,
            level=level,
            status=status,
            trace_id=context.trace_id,
            workflow_id=self.workflow_id,
            task_id=self.request.metadata.get("task_id", ""),
            run_id=context.run_id,
            parent_run_id=self.parent_run_id,
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
            "workflow_id": self.workflow_id,
            "run_id": context.run_id,
            "parent_run_id": self.parent_run_id,
            "runtime_call_id": str(native_run_id),
            "parent_runtime_call_id": str(parent_run_id or ""),
            "span_id": str(native_run_id),
            "parent_span_id": str(parent_run_id or ""),
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


class DeepAgentGraphTelemetry(GraphCallbackHandler):
    """Project LangGraph interrupt/resume lifecycle without graph state content."""

    def __init__(self, request: RunRequest, audit_sink: AuditSink | None = None) -> None:
        self.request = request
        self.audit_sink = audit_sink
        self.workflow_id = log_context.get(
            "workflow_id", request.context.run_id or request.context.trace_id
        )
        self.parent_run_id = log_context.get("parent_run_id")

    def on_interrupt(self, event: GraphInterruptEvent) -> None:
        self._record(
            "runtime.graph.interrupted",
            "waiting_approval",
            event,
            interrupt_count=len(event.interrupts),
        )

    def on_resume(self, event: GraphResumeEvent) -> None:
        self._record("runtime.graph.resumed", "running", event)

    def _record(
        self,
        event_name: str,
        status: str,
        event: GraphInterruptEvent | GraphResumeEvent,
        *,
        interrupt_count: int = 0,
    ) -> None:
        context = self.request.context
        namespace = tuple(str(item) for item in event.checkpoint_ns)
        fields: dict[str, Any] = {
            "trace_id": context.trace_id,
            "workflow_id": self.workflow_id,
            "task_id": self.request.metadata.get("task_id", ""),
            "run_id": context.run_id,
            "parent_run_id": self.parent_run_id,
            "thread_id": context.run_id or context.trace_id,
            "checkpoint_id": event.checkpoint_id,
            "span_id": str(event.run_id or ""),
            "actor_id": context.actor_id,
            "conversation_id": context.conversation_id,
            "agent_ref": self.request.metadata.get("agent_ref", ""),
            "skill_ref": self.request.metadata.get("skill_ref", ""),
            "policy_ref": self.request.metadata.get("policy_ref", ""),
            "graph_status": event.status,
            "checkpoint_ns_sha256": safe_hash(namespace),
            "checkpoint_ns_depth": len(namespace),
            "interrupt_count": interrupt_count,
        }
        log_event(log, event_name, status=status, **fields)
        if self.audit_sink is None:
            return
        try:
            self.audit_sink.append_audit(
                "runtime.graph.lifecycle",
                actor_id=context.actor_id,
                scope_id=context.scope,
                trace_id=context.trace_id,
                target=event.checkpoint_id,
                decision=status,
                details={
                    "schema_version": "1.0",
                    "event": event_name,
                    "workflow_id": fields["workflow_id"],
                    "run_id": context.run_id,
                    "parent_run_id": fields["parent_run_id"],
                    "thread_id": fields["thread_id"],
                    "checkpoint_id": event.checkpoint_id,
                    "span_id": fields["span_id"],
                    "graph_status": event.status,
                    "checkpoint_ns_sha256": fields["checkpoint_ns_sha256"],
                    "checkpoint_ns_depth": fields["checkpoint_ns_depth"],
                    "interrupt_count": interrupt_count,
                },
            )
        except Exception as error:
            log_event(
                log,
                "audit.write_failed",
                level=logging.ERROR,
                status="degraded",
                error_code=type(error).__name__,
                checkpoint_id=event.checkpoint_id,
            )


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


def _native_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project only bounded, content-free LangGraph/LangChain callback metadata."""
    if not metadata:
        return {}
    projected: dict[str, Any] = {}
    for source, target in (
        ("langgraph_node", "graph_node"),
        ("langgraph_step", "graph_step"),
        ("ls_provider", "model_provider"),
        ("ls_model_name", "model_name"),
    ):
        value = metadata.get(source)
        if isinstance(value, (str, int)) and value not in ("", None):
            projected[target] = value
    return projected
