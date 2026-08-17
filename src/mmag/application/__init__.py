"""Application composition and Mattermost adapters."""

from .actions import ActionCallbackServer, ActionClaims, ActionTokenError, ActionTokenService
from .app import Agent
from .context import (
    AttachmentProcessor,
    BotIdentity,
    ContextBuilder,
    format_time_label,
    is_text_attachment,
)
from .delivery import MattermostDelivery
from .goal_ui import GoalWorkspaceUI
from .message_handler import MessageHandler
from .probe import MattermostCapabilities, MattermostCapabilityProbe
from .render import MattermostRenderer, RenderedResponse, split_markdown
from .stream import MattermostStream
from .task_drafts import TaskDraftCoordinator
from .views import (
    ResponseAction,
    ResponseArtifact,
    ResponseKind,
    ResponsePresenter,
    ResponseSection,
    ResponseSource,
    ResponseView,
    RunStatus,
)

__all__ = [
    "Agent",
    "ActionCallbackServer",
    "ActionClaims",
    "ActionTokenError",
    "ActionTokenService",
    "AttachmentProcessor",
    "BotIdentity",
    "ContextBuilder",
    "MattermostDelivery",
    "GoalWorkspaceUI",
    "MattermostCapabilities",
    "MattermostCapabilityProbe",
    "MessageHandler",
    "MattermostRenderer",
    "MattermostStream",
    "TaskDraftCoordinator",
    "RenderedResponse",
    "ResponseAction",
    "ResponseArtifact",
    "ResponseKind",
    "ResponsePresenter",
    "ResponseSection",
    "ResponseSource",
    "ResponseView",
    "RunStatus",
    "format_time_label",
    "is_text_attachment",
    "split_markdown",
]
