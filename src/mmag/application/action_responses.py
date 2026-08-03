"""Preserve Mattermost post content when handling interactive actions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


async def load_action_post(
    client,
    payload: dict[str, Any],
    *,
    channel_id: str,
    bot_user_id: str,
) -> dict[str, Any]:
    """Load and validate the bot post that owns an interactive action."""
    post_id = str(payload.get("post_id") or "")
    if not post_id:
        raise ValueError("Mattermost action post ID is required")
    post = await client.get_post_async(post_id)
    if str(post.get("channel_id") or "") != channel_id:
        raise PermissionError("action post belongs to another conversation")
    if str(post.get("user_id") or "") != bot_user_id:
        raise PermissionError("action post is not owned by this bot")
    return post


def preserve_action_post(
    post: dict[str, Any],
    payload: dict[str, Any],
    feedback: str,
    *,
    terminal: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    """Keep the original body while retiring used interactive controls."""
    context = payload.get("context")
    clicked_token = str(context.get("token") or "") if isinstance(context, dict) else ""
    props = deepcopy(post.get("props")) if isinstance(post.get("props"), dict) else {}
    attachments = props.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            actions = attachment.get("actions")
            if not isinstance(actions, list):
                continue
            remaining = [] if terminal else [
                action for action in actions if _action_token(action) != clicked_token
            ]
            if remaining:
                attachment["actions"] = remaining
            else:
                attachment.pop("actions", None)
                attachment["text"] = feedback
    if status is not None:
        props["mmag_status"] = status
    return {
        "update": {
            "message": str(post.get("message") or ""),
            "props": props,
        },
        "ephemeral_text": feedback,
        "skip_slack_parsing": True,
    }


def _action_token(action: Any) -> str:
    if not isinstance(action, dict):
        return ""
    integration = action.get("integration")
    if not isinstance(integration, dict):
        return ""
    context = integration.get("context")
    return str(context.get("token") or "") if isinstance(context, dict) else ""
