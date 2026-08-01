"""Artifact-only file delivery capability contract."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from mmag.capabilities import (
    CapabilityContext,
    CapabilityEffect,
    create_send_file_capability,
)

_REF = "artifact://0123456789abcdef0123456789abcdef"


class FakeArtifacts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def resolve(self, ref: str, *, scope_id: str):
        self.calls.append((ref, scope_id))
        return (
            SimpleNamespace(
                ref=ref,
                filename="deck.pptx",
                media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            None,
        )


def _context(message: str = "请把 PPT 发给我") -> CapabilityContext:
    return CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="channel-1",
        message_id="post-1",
        message=message,
        scope="mattermost:team-1/channel-1",
        allowed_capabilities=frozenset({"send_file"}),
    )


def test_send_file_is_an_approval_gated_artifact_intent():
    spec = create_send_file_capability(FakeArtifacts(), context_provider=_context)

    assert spec.name == "send_file"
    assert spec.effect is CapabilityEffect.WRITE
    assert spec.permission == "mattermost:file:write"
    assert spec.input_schema["required"] == ("artifact_ref",)
    assert "content" not in spec.input_schema["properties"]


@pytest.mark.asyncio
async def test_send_file_returns_delivery_intent_without_platform_io():
    artifacts = FakeArtifacts()
    spec = create_send_file_capability(artifacts, context_provider=_context)

    result = await spec.handler(_REF, "演示文稿")

    assert artifacts.calls == [(_REF, "mattermost:team-1/channel-1")]
    assert result == {
        "success": True,
        "deliveries": [
            {
                "artifact_ref": _REF,
                "filename": "deck.pptx",
                "media_type": (
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ),
                "message": "演示文稿",
            }
        ],
    }


@pytest.mark.asyncio
async def test_send_file_requires_explicit_user_intent():
    artifacts = FakeArtifacts()
    spec = create_send_file_capability(
        artifacts,
        context_provider=lambda: _context("做一份 PPT"),
    )

    result = await spec.handler(_REF)

    assert "error" in result
    assert artifacts.calls == []


@pytest.mark.asyncio
async def test_send_file_treats_ppt_generation_as_delivery_intent():
    artifacts = FakeArtifacts()
    context = replace(
        _context("做一份 PPT"),
        allowed_capabilities=frozenset({"ppt.build", "send_file"}),
    )
    spec = create_send_file_capability(artifacts, context_provider=lambda: context)

    result = await spec.handler(_REF)

    assert result["success"] is True
    assert artifacts.calls == [(_REF, "mattermost:team-1/channel-1")]


@pytest.mark.asyncio
async def test_send_file_fails_closed_without_repository():
    spec = create_send_file_capability(None, context_provider=_context)

    result = await spec.handler(_REF)

    assert result == {"error": "Artifact Repository 未配置。"}
