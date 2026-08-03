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
from mmag.application.action_responses import preserve_action_post
from mmag.application.views import ResponseKind, ResponseSection
from mmag.control_plane import InboundEvent, OutboundMessage, SQLiteControlPlane


def test_report_presenter_exposes_readable_contract_not_raw_json():
    output = AgentOutput(
        text='{"title":"raw"}',
        agent_name="report",
        result={
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


def test_renderer_preserves_markdown_for_agent_content():
    """agent 写的 Markdown 应原样进入 message，由 Mattermost marked 渲染，不被反斜杠转义。"""
    view = ResponseView(
        kind=ResponseKind.RESULT,
        title="标题",
        summary="**粗体** 与 `代码` 和 [链接](https://example.com)",
        status=RunStatus.SUCCEEDED,
        sections=(
            ResponseSection(
                "分析",
                body="| a | b |\n|---|---|\n| 1 | 2 |",
                items=("~~删除线~~ 项",),
            ),
        ),
    )
    markdown = MattermostRenderer().render(view).chunks[0]

    assert "**粗体**" in markdown
    assert "`代码`" in markdown
    assert "[链接](https://example.com)" in markdown
    assert "~~删除线~~" in markdown
    assert "| a | b |" in markdown
    # 回归保护:不得出现反斜杠转义的 md 字符(否则 Mattermost 会当字面量)
    assert "\\*" not in markdown
    assert "\\`" not in markdown
    assert "\\[" not in markdown


def test_renderer_maps_internal_action_names_to_mattermost_route_ids():
    renderer = MattermostRenderer(action_callback_url="https://mmag.example.com/actions")
    view = ResponseView(
        kind=ResponseKind.STATUS,
        title="个人工作台",
        summary="请选择",
        status=RunStatus.SUCCEEDED,
        actions=(
            ResponseAction("pskill_run", "运行", "pskill_run", "skill-1", token="one"),
            ResponseAction("pskillrun", "另一个", "other", "skill-2", token="two"),
        ),
    )

    actions = renderer.render(view).actions

    assert all(action["id"].isalnum() for action in actions)
    assert all(len(action["id"]) <= 64 for action in actions)
    assert actions[0]["id"] != actions[1]["id"]
    assert all(action["type"] == "button" for action in actions)
    assert actions[0]["integration"]["context"] == {"token": "one"}


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


def test_action_feedback_preserves_original_post_and_only_retires_clicked_button():
    post = {
        "message": "完整研究结论",
        "props": {
            "mmag_kind": "result",
            "attachments": [{
                "text": "请选择操作",
                "actions": [
                    {"integration": {"context": {"token": "save"}}},
                    {"integration": {"context": {"token": "draft"}}},
                ],
            }],
        },
    }

    response = preserve_action_post(
        post, {"context": {"token": "save"}}, "案例已保存。"
    )

    assert response["update"]["message"] == "完整研究结论"
    actions = response["update"]["props"]["attachments"][0]["actions"]
    assert actions == [{"integration": {"context": {"token": "draft"}}}]
    assert response["ephemeral_text"] == "案例已保存。"
    assert post["props"]["attachments"][0]["actions"][0]["integration"] == {
        "context": {"token": "save"}
    }


def test_terminal_action_preserves_body_and_replaces_buttons_with_status():
    post = {
        "message": "审批对象、风险和来源",
        "props": {"attachments": [{"text": "请选择", "actions": [{"id": "approve"}]}]},
    }

    response = preserve_action_post(
        post, {"context": {"token": "approve"}}, "已批准。", terminal=True
    )

    assert response["update"]["message"] == "审批对象、风险和来源"
    attachment = response["update"]["props"]["attachments"][0]
    assert "actions" not in attachment
    assert attachment["text"] == "已批准。"


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
    assert delivery.actor_id == "user-1"
    store.close()
