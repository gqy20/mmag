"""Serializable LangGraph state and small transformation helpers."""

from __future__ import annotations

import operator
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

if TYPE_CHECKING:
    from .base import RunRequest


class LangGraphState(TypedDict):
    messages: Annotated[list[dict[str, Any]], operator.add]
    system_prompt: str
    capabilities: list[dict[str, Any]]
    max_rounds: int
    max_tokens: int
    round: int
    final_text: str
    thread_id: str
    review_decisions: dict[str, dict[str, Any]]


def last_tool_calls(state: LangGraphState) -> list[dict[str, Any]]:
    content = state["messages"][-1].get("content", [])
    return [item for item in content if isinstance(item, dict) and item.get("type") == "tool_use"]


def tool_result(tool_call_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": content}],
    }


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def thaw_messages(messages: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
    return [dict(thaw(message)) for message in messages]


def remaining_seconds(request: RunRequest) -> float | None:
    deadline = request.context.deadline
    if deadline is None:
        return None
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
    return (deadline - now).total_seconds()
