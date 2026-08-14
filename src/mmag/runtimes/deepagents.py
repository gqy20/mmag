"""Deep Agents harness behind MMAG's provider-neutral runtime contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import ToolRuntime  # noqa: TC002 - runtime injection inspects this annotation
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import SecretStr

from ..capabilities import (
    AuthorizationDecision,
    CapabilityResult,
    CapabilityStatus,
    bind_capability_context,
    get_capability_context,
)
from ..config import config
from ..governance import get_governance_context
from ..logger import get_logger, log_context, log_event, safe_hash
from ..model_artifacts import strip_model_artifacts
from ..skill_packages import get_skill_context
from .base import (
    AgentResult,
    AgentRuntimeError,
    RunContext,
    RunEvent,
    RunEventKind,
    RunRequest,
    RuntimeStatus,
    RuntimeTimeoutError,
    TokenUsage,
    remaining_seconds,
    thaw,
    translate_runtime_error,
)
from .harness import (
    build_run_limit_middleware,
    build_state_filesystem_permissions,
    build_tool_discovery,
    build_tool_visibility_middleware,
    build_workspace_interrupt_rules,
)
from .outputs import repair_structured_output
from .telemetry import AuditSink, DeepAgentTelemetry

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from ..capabilities import CapabilityRegistry
    from ..execution import WorkspaceBackendFactory


_PROFILE_REGISTERED = False
log = get_logger(__name__)


def _register_mmag_profile() -> None:
    global _PROFILE_REGISTERED  # noqa: PLW0603
    if _PROFILE_REGISTERED:
        return
    register_harness_profile(
        "anthropic",
        HarnessProfile(
            excluded_tools=frozenset(),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _PROFILE_REGISTERED = True


class ManagedChatModelFactory:
    """Create explicit Anthropic models without exposing provider choices to YAML."""

    def __init__(self, model_classes: Mapping[str, str] | None = None) -> None:
        self.model_classes = dict(
            model_classes
            or {
                "low-reasoning": config.anthropic_low_model,
                "medium-reasoning": config.anthropic_medium_model,
            }
        )
        if not self.model_classes or any(not name for name in self.model_classes.values()):
            raise ValueError("every model class must map to a concrete model")
        self._models: dict[tuple[str, int, float], BaseChatModel] = {}

    def resolve(self, model_class: str) -> str:
        try:
            return self.model_classes[model_class]
        except KeyError as error:
            raise ValueError(f"unsupported model class {model_class!r}") from error

    def validate(self, *, model_class: str, max_tokens: int, temperature: float) -> None:
        self.resolve(model_class)
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if not 0 <= temperature <= 1:
            raise ValueError("Anthropic temperature must be between 0 and 1")

    def create(self, *, model_class: str, max_tokens: int, temperature: float) -> BaseChatModel:
        self.validate(
            model_class=model_class,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        model_name = self.resolve(model_class)
        key = (model_name, max_tokens, temperature)
        model = self._models.get(key)
        if model is None:
            model = ChatAnthropic(
                model_name=model_name,
                api_key=SecretStr(config.anthropic_api_key),
                base_url=config.anthropic_base_url,
                max_tokens_to_sample=max_tokens,
                temperature=temperature,
                timeout=None,
                max_retries=0,
                stop=None,
                streaming=True,
            )
            self._models[key] = model
        return model


@dataclass(slots=True)
class _RunSession:
    graph: CompiledStateGraph[Any, Any, Any, Any]
    request: RunRequest
    calls: list[Mapping[str, Any]]
    artifacts: list[Mapping[str, Any]]
    deliveries: list[Mapping[str, Any]]


class DeepAgentRuntime:
    """Use Deep Agents as the default LangGraph harness for model-driven runs."""

    runtime_name = "deepagents"

    def __init__(
        self,
        capability_registry: CapabilityRegistry,
        *,
        checkpoint_path: str | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        model_factory: ManagedChatModelFactory | None = None,
        audit_sink: AuditSink | None = None,
        workspace_backend_factory: WorkspaceBackendFactory | None = None,
    ) -> None:
        _register_mmag_profile()
        self.capability_registry = capability_registry
        self.checkpoint_path = checkpoint_path
        self.model_factory = model_factory or ManagedChatModelFactory()
        self.audit_sink = audit_sink
        self.workspace_backend_factory = workspace_backend_factory
        self._checkpointer = checkpointer
        self._checkpoint_context: AbstractAsyncContextManager[Any] | None = None
        self._sessions: dict[str, _RunSession] = {}
        if self._checkpointer is None and checkpoint_path is None:
            self._checkpointer = InMemorySaver()

    async def start(self) -> None:
        if self._checkpointer is not None:
            return
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self._checkpoint_context = AsyncSqliteSaver.from_conn_string(
            cast("str", self.checkpoint_path)
        )
        self._checkpointer = await self._checkpoint_context.__aenter__()
        setup = getattr(self._checkpointer, "setup", None)
        if setup is not None:
            await setup()

    async def close(self) -> None:
        if self.workspace_backend_factory is not None:
            for session in self._sessions.values():
                self.workspace_backend_factory.release(session.request)
        self._sessions.clear()
        context, self._checkpoint_context = self._checkpoint_context, None
        self._checkpointer = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def run(self, request: RunRequest) -> AgentResult:
        remaining = remaining_seconds(request)
        if remaining is not None and remaining <= 0:
            raise RuntimeTimeoutError("runtime deadline exceeded", runtime=self.runtime_name)
        try:
            if remaining is None:
                return await self._run(request)
            async with asyncio.timeout(remaining):
                return await self._run(request)
        except AgentRuntimeError:
            raise
        except TimeoutError as error:
            raise RuntimeTimeoutError(
                "runtime deadline exceeded", runtime=self.runtime_name
            ) from error
        except Exception as error:
            translated = translate_runtime_error(error, runtime=self.runtime_name)
            raise translated from error

    def resolve_model(self, request: RunRequest) -> str:
        return self.model_factory.resolve(str(request.metadata.get("model_class") or ""))

    def validate_model_policy(
        self, *, model_class: str, max_tokens: int, temperature: float
    ) -> None:
        self.model_factory.validate(
            model_class=model_class,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def _run(self, request: RunRequest) -> AgentResult:
        await self.start()
        thread_id = request.context.run_id or request.context.trace_id
        session = self._build_session(request)
        self._sessions[thread_id] = session
        state: dict[str, Any] = {
            "messages": [thaw(message) for message in request.messages],
        }
        if request.skill_files:
            state["files"] = thaw(request.skill_files)
        try:
            result = await self._invoke(
                session.graph,
                state,
                self._config(thread_id, request),
                request,
            )
        except Exception:
            self._sessions.pop(thread_id, None)
            self._release_workspace(request)
            raise
        converted = await self._to_result(
            result,
            session,
            thread_id,
            request.event_sink,
            request,
        )
        if converted.status is not RuntimeStatus.WAITING_APPROVAL:
            self._sessions.pop(thread_id, None)
            self._release_workspace(request)
        return converted

    async def resume(self, thread_id: str, decision: Mapping[str, Any]) -> AgentResult:
        command = dict(decision)
        snapshot = command.pop("runtime_snapshot", None)
        access_context = command.pop("access_context", None)
        session = self._sessions.get(thread_id)
        if session is None:
            if not isinstance(snapshot, Mapping):
                raise RuntimeError("Deep Agents resume is missing its durable runtime snapshot")
            request, calls, artifacts, deliveries = _restore_runtime_snapshot(snapshot)
            await self.start()
            session = self._build_session(
                request,
                calls=calls,
                artifacts=artifacts,
                deliveries=deliveries,
            )
            self._sessions[thread_id] = session
        _verify_resume_access(session.request, access_context)
        try:
            result = await session.graph.ainvoke(
                Command[Any](resume=thaw(command)),
                self._config(thread_id, session.request),
            )
        except AgentRuntimeError:
            raise
        except Exception as error:
            self._sessions.pop(thread_id, None)
            self._release_workspace(session.request)
            translated = translate_runtime_error(error, runtime=self.runtime_name)
            raise translated from error
        converted = await self._to_result(result, session, thread_id, None, session.request)
        if converted.status is not RuntimeStatus.WAITING_APPROVAL:
            self._sessions.pop(thread_id, None)
            self._release_workspace(session.request)
        return converted

    def _release_workspace(self, request: RunRequest) -> None:
        if self.workspace_backend_factory is not None:
            self.workspace_backend_factory.release(request)

    def _build_session(
        self,
        request: RunRequest,
        *,
        calls: list[Mapping[str, Any]] | None = None,
        artifacts: list[Mapping[str, Any]] | None = None,
        deliveries: list[Mapping[str, Any]] | None = None,
    ) -> _RunSession:
        calls = calls or []
        artifacts = artifacts or []
        deliveries = deliveries or []
        workspace_enabled = bool(
            self.workspace_backend_factory and self.workspace_backend_factory.is_enabled(request)
        )
        tools = [
            self._tool(schema, calls, artifacts, deliveries, request)
            for schema in request.capabilities
            if str(schema["name"]) not in {"workspace.read", "workspace.write", "workspace.execute"}
            and (str(schema["name"]) != "workspace.commit" or workspace_enabled)
        ]
        discovery_tools, discovery_middleware, catalog_prompt = build_tool_discovery(request.capabilities)
        tools.extend(discovery_tools)
        interrupt_capabilities = tuple(
            schema
            for schema in request.capabilities
            if workspace_enabled or not str(schema["name"]).startswith("workspace.")
        )
        interrupt_on = self._interrupt_rules(interrupt_capabilities)
        response_format = (
            ToolStrategy(thaw(request.response_schema))
            if request.response_schema is not None
            else None
        )
        backend = (
            self.workspace_backend_factory.create(request)
            if self.workspace_backend_factory is not None
            else StateBackend()
        )
        graph = create_deep_agent(
            model=self.model_factory.create(
                model_class=str(request.metadata.get("model_class") or ""),
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            ),
            tools=tools,
            system_prompt=request.system_prompt + catalog_prompt,
            backend=backend,
            skills=["/skills/"] if request.skill_files else None,
            subagents=[],
            middleware=(
                *build_run_limit_middleware(request),
                *discovery_middleware,
                *build_tool_visibility_middleware(
                    request,
                    execute_enabled=workspace_enabled,
                ),
            ),
            permissions=build_state_filesystem_permissions(),
            interrupt_on=interrupt_on or None,
            response_format=response_format,
            checkpointer=self._checkpointer,
            name="mmag-agent",
        )
        return _RunSession(graph, request, calls, artifacts, deliveries)

    def _tool(
        self,
        schema: Mapping[str, Any],
        calls: list[Mapping[str, Any]],
        artifacts: list[Mapping[str, Any]],
        deliveries: list[Mapping[str, Any]],
        request: RunRequest,
    ) -> StructuredTool:
        name = str(schema["name"])

        async def invoke(runtime: ToolRuntime, **arguments: Any) -> str:
            await self._emit(request, RunEventKind.TOOL_STARTED, name=name)
            current_context = get_capability_context()
            tool_call_id = str(runtime.tool_call_id or "")
            execution_key = ""
            if current_context is not None and tool_call_id:
                identity = "\0".join(
                    (
                        current_context.installation_id,
                        current_context.tenant_id,
                        current_context.scope,
                        current_context.actor_id,
                        current_context.run_id or current_context.trace_id,
                        name,
                        tool_call_id,
                    )
                )
                execution_key = hashlib.sha256(identity.encode()).hexdigest()
            bound_context = (
                replace(
                    current_context,
                    tool_call_id=tool_call_id,
                    execution_key=execution_key,
                )
                if current_context is not None
                else None
            )
            context_manager = (
                bind_capability_context(bound_context)
                if bound_context is not None
                else nullcontext()
            )
            with context_manager:
                with log_context.bind(authorization_phase="tool_execute"):
                    authorization = self.capability_registry.authorization(name, arguments)
                if authorization.decision is AuthorizationDecision.DENY:
                    result = CapabilityResult(
                        CapabilityStatus.FORBIDDEN,
                        message=authorization.reason,
                    )
                else:
                    result = await self.capability_registry.execute(
                        name,
                        arguments,
                        preauthorized=True,
                    )
            payload = result.to_payload()
            calls.append(
                {
                    "name": name,
                    "tool_call_id": tool_call_id,
                    "execution_key_sha256": safe_hash(execution_key) if execution_key else "",
                    "arguments": dict(arguments),
                    "result": payload,
                    "status": result.status.value,
                    "duration_ms": result.duration_ms,
                    "error_code": (result.status.value if result.status.value != "success" else ""),
                }
            )
            log_event(
                log,
                "runtime.tool.result",
                status=result.status.value,
                capability=name,
                tool_call_id_sha256=safe_hash(tool_call_id) if tool_call_id else "",
                execution_key_sha256=safe_hash(execution_key) if execution_key else "",
                duration_ms=result.duration_ms,
            )
            if isinstance(payload, dict):
                artifacts.extend(_mapping_items(payload.get("artifacts")))
                deliveries.extend(_mapping_items(payload.get("deliveries")))
            event_kind = (
                RunEventKind.TOOL_COMPLETED
                if result.status is CapabilityStatus.SUCCESS
                else RunEventKind.TOOL_FAILED
            )
            await self._emit(
                request,
                event_kind,
                name=name,
                data={"status": result.status.value},
            )
            return _provider_tool_content(payload)

        return StructuredTool.from_function(
            coroutine=invoke,
            name=name,
            description=str(schema.get("description") or name),
            args_schema=thaw(schema.get("input_schema") or {"type": "object"}),
        )

    def _interrupt_rules(
        self, capabilities: tuple[Mapping[str, Any], ...]
    ) -> dict[str, bool | InterruptOnConfig]:
        rules: dict[str, bool | InterruptOnConfig] = {}
        for schema in capabilities:
            name = str(schema["name"])
            if name.startswith("workspace."):
                continue
            binding = self.capability_registry.get(name)
            if binding.capability is None:
                continue

            def requires_approval(tool_request, capability_name=name):
                arguments = dict(tool_request.tool_call.get("args") or {})
                with log_context.bind(authorization_phase="interrupt_check"):
                    authorization = self.capability_registry.authorization(
                        capability_name,
                        arguments,
                    )
                return bool(
                    authorization
                    and authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL
                )

            rules[name] = InterruptOnConfig(
                allowed_decisions=["approve", "reject"],
                when=requires_approval,
            )
        rules.update(build_workspace_interrupt_rules(capabilities, self.capability_registry))
        return rules

    async def _invoke(self, graph, state, graph_config, request: RunRequest) -> dict[str, Any]:
        if request.event_sink is None:
            return await graph.ainvoke(state, graph_config)
        final: dict[str, Any] = {}
        async for mode, item in graph.astream(
            state,
            graph_config,
            stream_mode=["messages", "values"],
        ):
            if mode == "values" and isinstance(item, dict):
                final = item
            elif mode == "messages":
                message = item[0] if isinstance(item, tuple) else item
                if isinstance(message, AIMessageChunk):
                    text = _content_text(message.content)
                    if text:
                        await self._emit(request, RunEventKind.TEXT_DELTA, text=text)
        snapshot = await graph.aget_state(graph_config)
        interrupts = tuple(
            interrupt for task in snapshot.tasks for interrupt in getattr(task, "interrupts", ())
        )
        if interrupts:
            final = {**final, "__interrupt__": interrupts}
        return final

    async def _to_result(
        self,
        state: Mapping[str, Any],
        session: _RunSession,
        thread_id: str,
        sink,
        request: RunRequest,
    ) -> AgentResult:
        raw_interrupts = state.get("__interrupt__", ())
        if raw_interrupts:
            messages = list(state.get("messages") or ())
            interruptions = tuple(
                _interrupt_payload(item, thread_id, request, session) for item in raw_interrupts
            )
            if sink is not None:
                await sink(RunEvent(RunEventKind.APPROVAL_REQUIRED))
            return AgentResult(
                "",
                self.runtime_name,
                status=RuntimeStatus.WAITING_APPROVAL,
                capability_calls=tuple(session.calls),
                interruptions=interruptions,
                usage=_usage(messages, tool_calls=len(session.calls)),
            )
        output = state.get("structured_response")
        if output is not None and hasattr(output, "model_dump"):
            output = output.model_dump()
        structured = dict(output) if isinstance(output, Mapping) else None
        repaired_fields = 0
        messages = list(state.get("messages") or ())
        _log_final_messages(messages, session)
        if structured is None and request.response_schema is not None:
            fallback = _text_result_fallback(messages, request.response_schema)
            if fallback is not None:
                structured = fallback
                log_event(
                    log,
                    "model.output_text_fallback",
                    status="succeeded",
                    field="text",
                )
        if structured is not None and request.response_schema is not None:
            structured, repaired_fields = repair_structured_output(
                structured,
                request.response_schema,
            )
            if repaired_fields:
                log_event(
                    log,
                    "model.output_repaired",
                    status="succeeded",
                    repaired_fields=repaired_fields,
                )
        text = _result_text(structured, messages)
        if structured is None and request.response_schema is None and text:
            structured = {"text": text}
        usage = _usage(
            messages,
            tool_calls=len(session.calls),
            repair_calls=1 if structured is not None and repaired_fields else 0,
        )
        return AgentResult(
            text,
            self.runtime_name,
            artifacts=tuple(session.artifacts),
            deliveries=tuple(session.deliveries),
            capability_calls=tuple(session.calls),
            usage=usage,
            output=structured,
        )

    def _config(self, thread_id: str, request: RunRequest) -> RunnableConfig:
        agent_ref = request.metadata.get("agent_ref", "mmag-agent")
        skill_ref = request.metadata.get("skill_ref", "")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(20, request.max_rounds * 8),
            "run_name": agent_ref,
            "tags": [
                "mmag",
                "deepagents",
                f"agent:{agent_ref}",
                *([f"skill:{skill_ref}"] if skill_ref else []),
            ],
            "callbacks": [DeepAgentTelemetry(request, self.audit_sink)],
            "metadata": {
                "trace_id": request.context.trace_id,
                "run_id": request.context.run_id,
                "thread_id": thread_id,
                "actor_id": request.context.actor_id,
                "scope": request.context.scope,
                "installation_id": request.context.installation_id,
                "tenant_id": request.context.tenant_id,
                "scope_kind": request.context.scope_kind,
                **dict(request.metadata),
            },
        }

    @staticmethod
    async def _emit(
        request: RunRequest,
        kind: RunEventKind,
        *,
        text: str = "",
        name: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if request.event_sink is not None:
            await request.event_sink(RunEvent(kind, text=text, name=name, data=data or {}))


def _log_final_messages(messages: list[Any], session: _RunSession) -> None:
    """Record content-free facts about the final model state."""
    last_ai: AIMessage | None = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_ai = msg
            break
    if last_ai is None:
        log_event(
            log,
            "runtime.output.observed",
            status="no_ai_message",
            message_count=len(messages),
        )
        return
    response_metadata = getattr(last_ai, "response_metadata", None) or {}
    stop_reason = response_metadata.get("stop_reason") or response_metadata.get("stop") or ""
    tool_calls = getattr(last_ai, "tool_calls", None) or []
    content = last_ai.content
    block_types: list[str] = []
    text_size = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                bt = str(block.get("type", "?"))
                if bt not in block_types:
                    block_types.append(bt)
                if bt == "text":
                    text_size += len(str(block.get("text") or ""))
    elif isinstance(content, str):
        block_types = ["text"]
        text_size = len(content)
    log_event(
        log,
        "runtime.output.observed",
        status="observed",
        message_count=len(messages),
        stop_reason=str(stop_reason),
        tool_call_names=[str(tc.get("name", "?")) for tc in tool_calls] if tool_calls else [],
        content_block_types=block_types,
        text_size=text_size,
        structured_output_requested=session.request.response_schema is not None,
        capability_calls=len(session.calls),
    )


def _provider_tool_content(payload: Any) -> str:
    """Serialize only at the LangChain/model ToolMessage boundary."""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") in {"text", "text_delta"}
    )


def _result_text(output: Mapping[str, Any] | None, messages: list[Any]) -> str:
    if output is not None:
        text = output.get("text")
        if isinstance(text, str):
            return strip_model_artifacts(text).strip()
        return json.dumps(output, ensure_ascii=False, default=str)
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _content_text(message.content)
            if text:
                return strip_model_artifacts(text).strip()
    return ""


def _text_result_fallback(messages: list[Any], schema: Mapping[str, Any]) -> dict[str, str] | None:
    """Project plain model text only into the narrow `{text: string}` contract."""
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, Mapping) or set(properties) != {"text"}:
        return None
    text_schema = properties.get("text")
    if not isinstance(text_schema, Mapping) or text_schema.get("type") != "string":
        return None
    if not isinstance(required, (list, tuple)) or set(required) != {"text"}:
        return None
    text = _result_text(None, messages)
    return {"text": text} if text else None


def _usage(messages: list[Any], *, tool_calls: int = 0, repair_calls: int = 0) -> TokenUsage:
    input_tokens = 0
    output_tokens = 0
    model_calls = 0
    for message in messages:
        metadata = getattr(message, "usage_metadata", None) or {}
        if isinstance(message, AIMessage) and metadata:
            model_calls += 1
        input_tokens += int(metadata.get("input_tokens") or 0)
        output_tokens += int(metadata.get("output_tokens") or 0)
    return TokenUsage(
        input_tokens,
        output_tokens,
        0.0,
        model_calls,
        tool_calls,
        repair_calls,
    )


def _interrupt_payload(
    interruption: Any,
    thread_id: str,
    request: RunRequest,
    session: _RunSession,
) -> Mapping[str, Any]:
    value = getattr(interruption, "value", {})
    value = dict(value) if isinstance(value, Mapping) else {}
    action_requests = value.get("action_requests", ())
    tool_calls = [
        {
            "tool_call_id": f"action-{index}",
            "capability": str(action.get("name") or ""),
            "arguments": dict(action.get("args") or {}),
            "reason": str(action.get("description") or ""),
        }
        for index, action in enumerate(action_requests)
        if isinstance(action, Mapping)
    ]
    governance = get_governance_context()
    capability = get_capability_context()
    skill = get_skill_context()
    governance_snapshot = (
        {
            "actor_id": governance.actor_id,
            "scope": governance.scope,
            "roles": sorted(governance.roles),
            "resources": dict(governance.resources),
            "policy_ref": governance.policy_ref,
            "allowed_capabilities": list(governance.allowed_capabilities),
        }
        if governance is not None
        else {}
    )
    return {
        "id": str(getattr(interruption, "id", "")),
        "value": {
            "runtime": "deepagents",
            "thread_id": thread_id,
            "tool_calls": tool_calls,
            "native_request": value,
            "runtime_snapshot": _runtime_snapshot(request, session),
            "governance_context": governance_snapshot,
            "execution_profiles": (
                sorted(capability.allowed_execution_profiles) if capability is not None else []
            ),
            "skill_context": skill.to_state() if skill is not None else {},
        },
    }


def _runtime_snapshot(request: RunRequest, session: _RunSession) -> Mapping[str, Any]:
    context = request.context
    return {
        "context": {
            "trace_id": context.trace_id,
            "actor_id": context.actor_id,
            "conversation_id": context.conversation_id,
            "scope": context.scope,
            "deadline": context.deadline.isoformat() if context.deadline else None,
            "run_id": context.run_id,
            "installation_id": context.installation_id,
            "tenant_id": context.tenant_id,
            "scope_kind": context.scope_kind,
            "owner_id": context.owner_id,
            "team_id": context.team_id,
            "channel_type": context.channel_type,
        },
        "messages": thaw(request.messages),
        "system_prompt": request.system_prompt,
        "capabilities": thaw(request.capabilities),
        "max_rounds": request.max_rounds,
        "max_tool_calls": request.max_tool_calls,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "response_schema": thaw(request.response_schema),
        "skill_files": thaw(request.skill_files),
        "metadata": thaw(request.metadata),
        "calls": thaw(session.calls),
        "artifacts": thaw(session.artifacts),
        "deliveries": thaw(session.deliveries),
    }


def _restore_runtime_snapshot(
    snapshot: Mapping[str, Any],
) -> tuple[
    RunRequest,
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
]:
    context = snapshot.get("context")
    if not isinstance(context, Mapping):
        raise ValueError("Deep Agents runtime snapshot has no context")
    deadline = context.get("deadline")
    request = RunRequest(
        context=RunContext(
            trace_id=str(context.get("trace_id") or ""),
            actor_id=str(context.get("actor_id") or ""),
            conversation_id=str(context.get("conversation_id") or ""),
            scope=str(context.get("scope") or ""),
            deadline=datetime.fromisoformat(deadline) if isinstance(deadline, str) else None,
            run_id=str(context.get("run_id") or ""),
            installation_id=str(context.get("installation_id") or ""),
            tenant_id=str(context.get("tenant_id") or ""),
            scope_kind=str(context.get("scope_kind") or ""),
            owner_id=str(context.get("owner_id") or ""),
            team_id=str(context.get("team_id") or ""),
            channel_type=str(context.get("channel_type") or ""),
        ),
        messages=tuple(snapshot.get("messages") or ()),
        system_prompt=str(snapshot.get("system_prompt") or ""),
        capabilities=tuple(snapshot.get("capabilities") or ()),
        max_rounds=int(snapshot.get("max_rounds") or 5),
        max_tool_calls=int(snapshot.get("max_tool_calls") or 50),
        max_tokens=int(snapshot.get("max_tokens") or 4096),
        temperature=float(snapshot.get("temperature") or 0.0),
        response_schema=snapshot.get("response_schema"),
        skill_files=snapshot.get("skill_files") or {},
        metadata=snapshot.get("metadata") or {},
    )
    return (
        request,
        _mapping_items(snapshot.get("calls")),
        _mapping_items(snapshot.get("artifacts")),
        _mapping_items(snapshot.get("deliveries")),
    )


def _verify_resume_access(
    request: RunRequest,
    access_context: Any,
) -> None:
    if not isinstance(access_context, Mapping):
        raise PermissionError("Deep Agents resume is missing its trusted access context")
    expected = {
        "actor_id": request.context.actor_id,
        "scope": request.context.scope,
        "installation_id": request.context.installation_id,
        "tenant_id": request.context.tenant_id,
    }
    for key, value in expected.items():
        if value and str(access_context.get(key) or "") != value:
            raise PermissionError(f"Deep Agents resume {key} does not match its checkpoint")
