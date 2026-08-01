"""Application composition and Mattermost adapters."""

from .app import Agent
from .context import (
    AttachmentProcessor,
    BotIdentity,
    ContextBuilder,
    format_time_label,
    is_text_attachment,
)
from .delivery import MattermostDelivery
from .message_handler import MessageHandler

__all__ = [
    "Agent",
    "AttachmentProcessor",
    "BotIdentity",
    "ContextBuilder",
    "MattermostDelivery",
    "MessageHandler",
    "format_time_label",
    "is_text_attachment",
]
