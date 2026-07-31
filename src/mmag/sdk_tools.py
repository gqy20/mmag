"""SDK bindings generated from the canonical capability catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .capabilities.bindings import bind_sdk_capability
from .capabilities.catalog import (
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_get_user_profile_capability,
    create_save_knowledge_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
)
from .capabilities.context import CapabilityContext, get_capability_context
from .capabilities.file import create_send_file_capability
from .capabilities.link import create_analyze_link_capability

if TYPE_CHECKING:
    from collections.abc import Callable


def create_sdk_tools(
    mm_client,
    memory,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
) -> list:
    """Build SDK tools from their single canonical specifications."""
    return [
        _make_sdk_get_posts(mm_client, memory),
        _make_sdk_search_messages(memory),
        _make_sdk_search_knowledge(memory),
        _make_sdk_get_channel_info(mm_client),
        _make_sdk_save_knowledge(memory),
        _make_sdk_get_user_profile(mm_client, memory),
        _make_sdk_analyze_link(memory),
        _make_sdk_send_file(mm_client, context_provider=context_provider),
    ]


def _make_sdk_get_posts(mm_client, memory):
    return bind_sdk_capability(create_get_posts_capability(mm_client, memory))


def _make_sdk_search_messages(memory):
    return bind_sdk_capability(create_search_messages_capability(memory))


def _make_sdk_search_knowledge(memory):
    return bind_sdk_capability(create_search_knowledge_capability(memory))


def _make_sdk_get_channel_info(mm_client):
    return bind_sdk_capability(create_get_channel_info_capability(mm_client))


def _make_sdk_save_knowledge(memory):
    return bind_sdk_capability(create_save_knowledge_capability(memory))


def _make_sdk_get_user_profile(mm_client, memory):
    return bind_sdk_capability(create_get_user_profile_capability(mm_client, memory))


def _make_sdk_analyze_link(memory):
    return bind_sdk_capability(create_analyze_link_capability(memory))


def _make_sdk_send_file(
    mm_client,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
):
    return bind_sdk_capability(
        create_send_file_capability(mm_client, context_provider=context_provider)
    )
