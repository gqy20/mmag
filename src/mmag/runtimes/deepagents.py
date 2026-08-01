"""Deep Agents harness behind MMAG's provider-neutral runtime contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
from langchain.agents.structured_output import ToolStrategy
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ..capabilities import AuthorizationDecision, get_capability_context
from ..config import config
from ..governance import get_governance_context
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

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from langchain_core.language_models import BaseChatModel
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from ..capabilities import CapabilityRegistry


_PROFILE_REGISTERED = False


def _register_mmag_profile() -> None:
    global _PROFILE_REGISTERED  # noqa: PLW0603
    if _PROFILE_REGISTERED:
        return
    register_harness_profile(
        "anthropic",
        HarnessProfile(
            excluded_tools=frozenset({"execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _PROFILE_REGISTERED = True


class ManagedChatModelFactory:
    """Create explicit Anthropic models without exposing provider choices to YAML."""

    def __init__(self) -> None:
        self._models: dict[tuple[int, float], BaseChatModel] = {}

    def create(self, *, max_tokens: int, temperature: float) -> BaseChatModel:
        key = (max_tokens, temperature)
        model = self._models.get(key)
        if model is None:
            model = ChatAnthropic(
                model_name=config.anthropic_model,
                api_key=config.anthropic_api_key,
                base_url=config.anthropic_base_url,
                max_tokens_to_sample=max_tokens,
                temperature=temperature,
                max_retries=0,
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
    ) -> None:
        _register_mmag_profile()
        self.capability_registry = capability_registry
        self.checkpoint_path = checkpoint_path
        self.model_factory = model_factory or ManagedChatModelFactory()
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
            raise RuntimeTimeoutError("runtime deadline exceeded", runtime=self.runtime_name) from error
        except Exception as error:
            translated = translate_runtime_error(error, runtime=self.runtime_name)
            raise translated from error

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
        result = await self._invoke(session.graph, state, self._config(thread_id, request), request)
        converted = await self._to_result(
            result,
            session,
            thread_id,
            request.event_sink,
            request,
        )
        if converted.status is not RuntimeStatus.WAITING_APPROVAL:
            self._sessions.pop(thread_id, None)
        return converted

    async def resume(self, thread_id: str, decision: Mapping[str, Any]) -> AgentResult:
        command = dict(decision)
        snapshot = command.pop("runtime_snapshot", None)
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
        result = await session.graph.ainvoke(
            Command(resume=thaw(command)),
            {"configurable": {"thread_id": thread_id}},
        )
        converted = await self._to_result(result, session, thread_id, None, session.request)
        if converted.status is not RuntimeStatus.WAITING_APPROVAL:
            self._sessions.pop(thread_id, None)
        return converted

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
        tools = [
            self._tool(schema, calls, artifacts, deliveries, request)
            for schema in request.capabilities
        ]
        interrupt_on = self._interrupt_rules(request.capabilities)
        response_format = (
            ToolStrategy(thaw(request.response_schema))
            if request.response_schema is not None
            else None
        )
        graph = create_deep_agent(
            model=self.model_factory.create(
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            ),
            tools=tools,
            system_prompt=request.system_prompt,
            backend=StateBackend(),
            skills=["/skills/"] if request.skill_files else None,
            subagents=[],
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

        async def invoke(**arguments: Any) -> str:
            await self._emit(request, RunEventKind.TOOL_STARTED, name=name)
            authorization = self.capability_registry.authorization(name, arguments)
            approved = bool(
                authorization
                and authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL
            )
            content = await self.capability_registry.execute(
                name,
                arguments,
                approval_granted=approved,
            )
            payload = _json_payload(content)
            calls.append({"name": name, "arguments": dict(arguments), "result": payload})
            if isinstance(payload, dict):
                artifacts.extend(_mapping_items(payload.get("artifacts")))
                deliveries.extend(_mapping_items(payload.get("deliveries")))
            await self._emit(request, RunEventKind.TOOL_COMPLETED, name=name)
            return content

        return StructuredTool.from_function(
            coroutine=invoke,
            name=name,
            description=str(schema.get("description") or name),
            args_schema=thaw(schema.get("input_schema") or {"type": "object"}),
        )

    def _interrupt_rules(
        self, capabilities: tuple[Mapping[str, Any], ...]
    ) -> dict[str, dict[str, Any]]:
        rules: dict[str, dict[str, Any]] = {}
        for schema in capabilities:
            name = str(schema["name"])
            binding = self.capability_registry.get(name)
            if binding.capability is None:
                continue

            def requires_approval(tool_request, capability_name=name):
                arguments = dict(tool_request.tool_call.get("args") or {})
                authorization = self.capability_registry.authorization(
                    capability_name, arguments
                )
                return bool(
                    authorization
                    and authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL
                )

            rules[name] = {
                "allowed_decisions": ["approve", "reject"],
                "when": requires_approval,
            }
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
            interrupt
            for task in snapshot.tasks
            for interrupt in getattr(task, "interrupts", ())
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
            interruptions = tuple(
                _interrupt_payload(item, thread_id, request, session)
                for item in raw_interrupts
            )
            if sink is not None:
                await sink(RunEvent(RunEventKind.APPROVAL_REQUIRED))
            return AgentResult(
                "",
                self.runtime_name,
                status=RuntimeStatus.WAITING_APPROVAL,
                capability_calls=tuple(session.calls),
                interruptions=interruptions,
            )
        output = state.get("structured_response")
        if hasattr(output, "model_dump"):
            output = output.model_dump()
        structured = dict(output) if isinstance(output, Mapping) else None
        messages = list(state.get("messages") or ())
        text = _result_text(structured, messages)
        usage = _usage(messages)
        return AgentResult(
            text,
            self.runtime_name,
            artifacts=tuple(session.artifacts),
            deliveries=tuple(session.deliveries),
            capability_calls=tuple(session.calls),
            usage=usage,
            output=structured,
        )

    @staticmethod
    def _config(thread_id: str, request: RunRequest) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max(20, request.max_rounds * 8),
            "metadata": {
                "trace_id": request.context.trace_id,
                "run_id": request.context.run_id,
                "actor_id": request.context.actor_id,
                "scope": request.context.scope,
            },
        }

    @staticmethod
    async def _emit(
        request: RunRequest,
        kind: RunEventKind,
        *,
        text: str = "",
        name: str = "",
    ) -> None:
        if request.event_sink is not None:
            await request.event_sink(RunEvent(kind, text=text, name=name))


def _json_payload(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


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


def _usage(messages: list[Any]) -> TokenUsage:
    input_tokens = 0
    output_tokens = 0
    for message in messages:
        metadata = getattr(message, "usage_metadata", None) or {}
        input_tokens += int(metadata.get("input_tokens") or 0)
        output_tokens += int(metadata.get("output_tokens") or 0)
    return TokenUsage(input_tokens, output_tokens, 0.0)


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
        },
        "messages": thaw(request.messages),
        "system_prompt": request.system_prompt,
        "capabilities": thaw(request.capabilities),
        "max_rounds": request.max_rounds,
        "max_tokens": request.max_tokens,
        "fallback_max_tokens": request.fallback_max_tokens,
        "temperature": request.temperature,
        "response_schema": thaw(request.response_schema),
        "skill_files": thaw(request.skill_files),
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
        ),
        messages=tuple(snapshot.get("messages") or ()),
        system_prompt=str(snapshot.get("system_prompt") or ""),
        capabilities=tuple(snapshot.get("capabilities") or ()),
        max_rounds=int(snapshot.get("max_rounds") or 5),
        max_tokens=int(snapshot.get("max_tokens") or 4096),
        fallback_max_tokens=int(snapshot.get("fallback_max_tokens") or 1024),
        temperature=float(snapshot.get("temperature") or 0.0),
        response_schema=snapshot.get("response_schema"),
        skill_files=snapshot.get("skill_files") or {},
    )
    return (
        request,
        _mapping_items(snapshot.get("calls")),
        _mapping_items(snapshot.get("artifacts")),
        _mapping_items(snapshot.get("deliveries")),
    )
