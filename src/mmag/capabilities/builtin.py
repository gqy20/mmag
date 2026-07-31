"""Single ordered catalog for mmag's built-in capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .catalog import (
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_get_user_profile_capability,
    create_save_knowledge_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
)
from .context import CapabilityContext, get_capability_context
from .file import create_send_file_capability
from .link import create_analyze_link_capability

if TYPE_CHECKING:
    from collections.abc import Callable

    from .base import CapabilitySpec


def create_builtin_capabilities(
    mm_client,
    memory,
    *,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
) -> list[CapabilitySpec]:
    """Create the ordered built-in catalog shared by every Runtime binding."""
    return [
        create_get_posts_capability(mm_client, memory),
        create_search_messages_capability(memory),
        create_search_knowledge_capability(memory),
        create_get_channel_info_capability(mm_client),
        create_save_knowledge_capability(memory),
        create_get_user_profile_capability(mm_client, memory),
        create_analyze_link_capability(memory),
        create_send_file_capability(mm_client, context_provider=context_provider),
    ]
