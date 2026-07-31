"""LangGraph tools generated from the single built-in capability catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..capabilities import bind_langgraph_capability, create_builtin_capabilities

if TYPE_CHECKING:
    from .registry import Tool


def build_builtin_tools(mm_client, memory, *, executor=None) -> list[Tool]:
    """Bind the canonical ordered catalog to LangGraph ToolRegistry tools."""
    return [
        bind_langgraph_capability(spec, executor=executor)
        for spec in create_builtin_capabilities(mm_client, memory)
    ]
