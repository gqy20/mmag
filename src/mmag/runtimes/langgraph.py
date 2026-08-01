"""Durable LangGraph runtime with native human-in-the-loop tool review."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..capabilities import AuthorizationDecision
from ..governance import get_governance_context
from ..llm import LLMError
from ..model_artifacts import strip_model_artifacts
from ..skill_packages import get_skill_resource_session
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
from .langgraph_state import (
    LangGraphState,
    last_tool_calls,
    thaw_messages,
    tool_result,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from ..capabilities import CapabilityRegistry
    from ..llm import LLM


_EXHAUSTED_TEXT = "⚠️ 处理超时，请重试"


class LangGraphRuntimeAdapter:
    """Default runtime: one compiled graph backed by a durable checkpointer."""

    runtime_name = "langgraph"

    def __init__(
        self,
        backend: LLM,
        *,
        capability_registry: CapabilityRegistry,
        checkpoint_path: str | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self.backend = backend
        self.capability_registry = capability_registry
        self.checkpoint_path = checkpoint_path
        self._checkpointer = checkpointer
        self._checkpoint_context: AbstractAsyncContextManager[Any] | None = None
        self._graph: CompiledStateGraph[Any, Any, Any, Any] | None = None
        if checkpointer is not None:
            self._graph = self._compile(checkpointer)
        elif checkpoint_path is None:
            self._graph = self._compile(InMemorySaver())

    async def start(self) -> None:
        if self._graph is not None:
            return
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self._checkpoint_context = AsyncSqliteSaver.from_conn_string(
            cast("str", self.checkpoint_path)
        )
        self._checkpointer = await self._checkpoint_context.__aenter__()
        self._graph = self._compile(self._checkpointer)

    async def close(self) -> None:
        context, self._checkpoint_context = self._checkpoint_context, None
        self._graph = None
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
        except Exception as error:
            translated = translate_runtime_error(error, runtime=self.runtime_name)
            raise translated from error

    async def _run(self, request: RunRequest) -> AgentResult:
        if not request.capabilities:
            text = await self.backend.chat(
                messages=thaw_messages(request.messages),
                system=request.system_prompt,
                max_tokens=request.max_tokens,
            )
            return AgentResult(text=text, runtime=self.runtime_name)

        graph = await self._ready_graph()
        thread_id = request.context.run_id or request.context.trace_id
        state: LangGraphState = {
            "messages": thaw_messages(request.messages),
            "system_prompt": request.system_prompt,
            "capabilities": [dict(thaw(item)) for item in request.capabilities],
            "max_rounds": request.max_rounds,
            "max_tokens": request.max_tokens,
            "round": 0,
            "final_text": "",
            "thread_id": thread_id,
            "review_decisions": {},
        }
        state_result = await graph.ainvoke(state, self._config(thread_id, request.max_rounds))
        result = self._to_result(state_result)
        if result.status is RuntimeStatus.EXHAUSTED:
            recovered = await recover_exhausted(
                self.backend,
                request,
                thaw_messages(request.messages),
                result.text,
            )
            if recovered != result.text:
                return AgentResult(text=recovered, runtime=self.runtime_name)
        return result

    async def resume(self, thread_id: str, decision: Mapping[str, Any]) -> AgentResult:
        """Resume the exact checkpoint suspended by a native LangGraph interrupt."""
        graph = await self._ready_graph()
        result = await graph.ainvoke(
            Command(resume=dict(decision)),
            self._config(thread_id),
        )
        return self._to_result(result)

    async def _ready_graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        await self.start()
        if self._graph is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("LangGraph runtime failed to initialize")
        return self._graph

    def _compile(self, checkpointer: BaseCheckpointSaver[Any]):
        graph = StateGraph(LangGraphState)
        graph.add_node("agent", self._agent_node)
        graph.add_node("review_tools", self._review_tools_node)
        graph.add_node("tools", self._tools_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent", self._after_agent, {END: END, "review_tools": "review_tools"}
        )
        graph.add_edge("review_tools", "tools")
        graph.add_edge("tools", "agent")
        return graph.compile(checkpointer=checkpointer, name="mmag-agent")

    async def _agent_node(self, state: LangGraphState) -> dict[str, Any]:
        round_number = state["round"] + 1
        final_round = round_number >= state["max_rounds"]
        try:
            parsed = await self.backend.complete(
                messages=state["messages"],
                system=state["system_prompt"],
                tools=[] if final_round else state["capabilities"],
                max_tokens=state["max_tokens"],
            )
        except Exception as error:
            if isinstance(error, LLMError):
                raise
            raise LLMError(f"Round {round_number} LLM 调用失败: {error}") from error

        text = strip_model_artifacts("\n".join(parsed.texts)).strip()
        if final_round or not parsed.tool_calls:
            return {"final_text": text or _EXHAUSTED_TEXT, "round": round_number}

        content: list[dict[str, Any]] = []
        if parsed.texts:
            content.append({"type": "text", "text": "\n".join(parsed.texts)})
        content.extend(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": call["name"],
                "input": call["input"],
            }
            for call in parsed.tool_calls
        )
        return {
            "messages": [{"role": "assistant", "content": content}],
            "round": round_number,
            "review_decisions": {},
        }

    def _review_tools_node(self, state: LangGraphState) -> dict[str, Any]:
        pending: list[dict[str, Any]] = []
        for call in last_tool_calls(state):
            authorization = self.capability_registry.authorization(call["name"], call["input"])
            if authorization and authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL:
                pending.append(
                    {
                        "tool_call_id": call["id"],
                        "capability": call["name"],
                        "arguments": call["input"],
                        "reason": authorization.reason,
                    }
                )
        if not pending:
            return {"review_decisions": {}}

        governance = get_governance_context()
        skill_resources = get_skill_resource_session()
        response = interrupt(
            {
                "kind": "tool_approval",
                "thread_id": state["thread_id"],
                "tool_calls": pending,
                "governance_context": {
                    "policy_ref": governance.policy_ref if governance is not None else "",
                    "allowed_capabilities": list(
                        governance.allowed_capabilities if governance is not None else ()
                    ),
                    "roles": list(governance.roles if governance is not None else ()),
                },
                "skill_resource_state": (
                    skill_resources.to_state() if skill_resources is not None else {}
                ),
            }
        )
        decisions = response.get("decisions", []) if isinstance(response, Mapping) else []
        return {
            "review_decisions": {
                str(item.get("tool_call_id")): dict(item)
                for item in decisions
                if isinstance(item, Mapping) and item.get("tool_call_id")
            }
        }

    async def _tools_node(self, state: LangGraphState) -> dict[str, Any]:
        new_messages: list[dict[str, Any]] = []
        for call in last_tool_calls(state):
            authorization = self.capability_registry.authorization(call["name"], call["input"])
            requires_approval = bool(
                authorization and authorization.decision is AuthorizationDecision.REQUIRE_APPROVAL
            )
            arguments = dict(call["input"])
            approval_granted = False
            if requires_approval:
                review = state["review_decisions"].get(call["id"], {})
                action = review.get("decision", "reject")
                if action == "edit" and isinstance(review.get("arguments"), Mapping):
                    arguments = dict(review["arguments"])
                    approval_granted = True
                elif action == "approve":
                    approval_granted = True
                else:
                    result = '{"error":{"code":"rejected","message":"Rejected by reviewer"}}'
                    new_messages.append(tool_result(call["id"], result))
                    continue

            result = await self.capability_registry.execute(
                call["name"], arguments, approval_granted=approval_granted
            )
            new_messages.append(tool_result(call["id"], result))
        return {"messages": new_messages, "review_decisions": {}}

    @staticmethod
    def _after_agent(state: LangGraphState) -> str:
        return END if state.get("final_text") else "review_tools"

    @staticmethod
    def _config(thread_id: str, max_rounds: int = 10) -> Any:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": max_rounds * 3 + 1,
        }

    def _to_result(self, state: Mapping[str, Any]) -> AgentResult:
        native_interrupts = state.get("__interrupt__", ())
        if native_interrupts:
            interruptions = tuple(
                {
                    "id": item.id,
                    "value": thaw(item.value),
                }
                for item in native_interrupts
            )
            return AgentResult(
                text="",
                runtime=self.runtime_name,
                status=RuntimeStatus.WAITING_APPROVAL,
                interruptions=interruptions,
            )
        text = str(state.get("final_text", "")) or _EXHAUSTED_TEXT
        status = (
            RuntimeStatus.EXHAUSTED if text.startswith("⚠️ 处理超时") else RuntimeStatus.COMPLETED
        )
        return AgentResult(text=text, runtime=self.runtime_name, status=status)
