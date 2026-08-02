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

    from ..control_plane import MattermostAccessGuard
    from ..execution import ArtifactRepository
    from .base import CapabilityExecutor, CapabilitySpec
    from .registry import CapabilityBinding


def create_builtin_capabilities(
    mm_client,
    memory,
    *,
    artifacts: ArtifactRepository | None = None,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
    access_guard: MattermostAccessGuard | None = None,
    additional_specs: tuple[CapabilitySpec, ...] = (),
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
        create_send_file_capability(
            artifacts,
            context_provider=context_provider,
            access_guard=access_guard,
        ),
        *additional_specs,
    ]


def build_builtin_bindings(
    mm_client,
    memory,
    *,
    artifacts: ArtifactRepository | None = None,
    executor: CapabilityExecutor,
    access_guard: MattermostAccessGuard | None = None,
    additional_specs: tuple[CapabilitySpec, ...] = (),
) -> list[CapabilityBinding]:
    """Project the canonical built-in catalog onto the default runtime surface."""
    from .bindings import bind_langgraph_capability

    return [
        bind_langgraph_capability(spec, executor=executor)
        for spec in create_builtin_capabilities(
            mm_client,
            memory,
            artifacts=artifacts,
            access_guard=access_guard,
            additional_specs=additional_specs,
        )
    ]
