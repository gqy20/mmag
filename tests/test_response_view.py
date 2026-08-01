"""Focused contracts for response presentation, rendering, and action tokens."""

import time

import pytest

from mmag.agent_system import AgentOutput
from mmag.application import (
    ActionTokenError,
    ActionTokenService,
    MattermostRenderer,
    ResponseAction,
    ResponsePresenter,
    ResponseView,
    RunStatus,
    split_markdown,
)
from mmag.application.views import ResponseKind
from mmag.control_plane import InboundEvent, OutboundMessage, SQLiteControlPlane


def test_report_presenter_exposes_readable_contract_not_raw_json():
    output = AgentOutput(
        text='{"title":"raw"}',
        agent_name="report",
        structured_result={
            "title": "市场研究",
            "executive_summary": "核心结论",
            "findings": [
                {"claim": "需求增长", "confidence": "high", "evidence": []}
            ],
            "recommendations": ["进入细分市场"],
            "limitations": ["样本有限"],
            "sources": [
                {
                    "id": "s1",
                    "title": "Source @all",
                    "ref": "https://example.com/report#fragment",
                    "source_type": "web",
                }
            ],
        },
    )

    view = ResponsePresenter().present(output, run_id="run-1")
    rendered = MattermostRenderer().render(view)
    markdown = rendered.chunks[0]

    assert view.title == "市场研究"
    assert "需求增长" in markdown
    assert '"findings"' not in markdown
    assert "@\u200ball" in markdown
    assert "#fragment" not in markdown


def test_markdown_split_closes_and_reopens_fences():
    markdown = "### Result\n\n```python\n" + "x = 1\n" * 300 + "```"

    chunks = split_markdown(markdown, 1_000)

    assert len(chunks) > 1
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)


def test_renderer_uses_text_fallback_without_callback():
    view = ResponseView(
        kind=ResponseKind.APPROVAL,
        title="审批",
        summary="需要确认",
        status=RunStatus.WAITING_APPROVAL,
        actions=(
            ResponseAction(
                "approve",
                "批准",
                "approve",
                "approval-1",
                fallback="`批准 approval-1`",
                token="token",
            ),
        ),
    )

    rendered = MattermostRenderer().render(view)

    assert "attachments" not in rendered.props
    assert "批准 approval-1" in rendered.chunks[0]


def test_action_token_is_signed_short_lived_and_one_time(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "control.db"))
    service = ActionTokenService("s" * 32, store, ttl_seconds=60)
    token = service.issue(
        action="approve",
        target="approval-1",
        scope_id="mattermost:team-1/channel-1",
        run_id="run-1",
        conversation_id="channel-1",
        root_id="post-1",
        requested_by="user-1",
    )

    claims = service.consume(token, actor_id="user-1")

    assert claims.action == "approve"
    assert claims.expires_at > time.time()
    with pytest.raises(ActionTokenError, match="already used"):
        service.consume(token, actor_id="user-1")
    with pytest.raises(ActionTokenError, match="signature"):
        service.verify(token[:-1] + ("A" if token[-1] != "A" else "B"))
    store.close()


def test_outbox_round_trips_thread_artifact_and_update_contract(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "outbox.db"))
    event = InboundEvent(
        "event-1",
        "mattermost",
        "posted",
        "channel-1",
        "user-1",
        time.time(),
        {},
    )
    assert store.accept_event(event)
    store.complete_event(
        event.event_id,
        (
            OutboundMessage(
                "channel-1",
                "result",
                root_id="post-1",
                message_kind="artifact",
                scope_id="mattermost:team-1/channel-1",
                artifact_refs=("artifact://0123456789abcdef0123456789abcdef",),
                file_ids=("file-1",),
                actions=({"id": "approve"},),
                update_post_id="status-1",
                idempotency_key="run-1:artifact",
            ),
        ),
    )

    delivery = store.list_deliveries()[0].message

    assert delivery.root_id == "post-1"
    assert delivery.message_kind == "artifact"
    assert delivery.artifact_refs[0].startswith("artifact://")
    assert delivery.file_ids == ("file-1",)
    assert delivery.actions == ({"id": "approve"},)
    assert delivery.update_post_id == "status-1"
    assert delivery.idempotency_key == "run-1:artifact"
    store.close()
