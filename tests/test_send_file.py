"""请求级文件能力与 Mattermost 文件发送契约。"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import MagicMock

import pytest

from mmag.capabilities import (
    CapabilityContext,
    CapabilityEffect,
    bind_capability_context,
    bind_sdk_capability,
    create_send_file_capability,
    get_capability_context,
)


def _make_mock_mm():
    mm = MagicMock()
    mm.upload_file.return_value = "file_id_abc"
    mm.send_post.return_value = "post_id_xyz"
    return mm


def _request_context(message: str = "@bot 帮我导出报告") -> CapabilityContext:
    return CapabilityContext(
        trace_id="trace-1",
        actor_id="user-1",
        conversation_id="ch1",
        message_id="post_001",
        message=message,
    )


async def _invoke(tool_fn, args, context: CapabilityContext | None):
    if context is None:
        return await tool_fn.handler(args)
    with bind_capability_context(context):
        return await tool_fn.handler(args)


def _run_tool(tool_fn, args, context: CapabilityContext | None = None):
    raw = asyncio.run(_invoke(tool_fn, args, context))
    return json.loads(raw["content"][0]["text"])


def _make_send_file_tool():
    mm = _make_mock_mm()
    spec = create_send_file_capability(mm)
    return bind_sdk_capability(spec), spec, mm


def _arguments(**overrides):
    values = {
        "filename": "报告.md",
        "content": "# 标题\n正文内容",
        "message": "这是你要的报告",
        "content_encoding": "text",
    }
    values.update(overrides)
    return values


def test_send_file_declares_governed_write_effect():
    _, spec, _ = _make_send_file_tool()

    assert spec.name == "send_file"
    assert spec.effect is CapabilityEffect.WRITE
    assert spec.permission == "mattermost:file:write"


@pytest.mark.parametrize(
    "message",
    [
        "帮我导出一下",
        "能不能发个文件给我",
        "please export as file",
        "PLEASE DOWNLOAD",
    ],
)
def test_explicit_file_request_succeeds(message):
    tool_fn, _, mm = _make_send_file_tool()

    result = _run_tool(tool_fn, _arguments(), _request_context(message))

    assert result["success"] is True
    assert result["filename"] == "报告.md"
    assert result["file_id"] == "file_id_abc"
    mm.upload_file.assert_called_once()


@pytest.mark.parametrize("context", [None, _request_context("今天天气怎么样")])
def test_missing_explicit_file_request_is_rejected(context):
    tool_fn, _, mm = _make_send_file_tool()

    result = _run_tool(tool_fn, _arguments(), context)

    assert "未明确请求" in result["error"]
    mm.upload_file.assert_not_called()


def test_base64_content_is_decoded_before_upload():
    tool_fn, _, mm = _make_send_file_tool()
    original = b"\x89PNG fake binary data"

    result = _run_tool(
        tool_fn,
        _arguments(
            filename="image.png",
            content=base64.b64encode(original).decode(),
            content_encoding="base64",
        ),
        _request_context("发文件给我"),
    )

    assert result["success"] is True
    assert mm.upload_file.call_args.args[2] == original


def test_oversized_file_is_rejected_before_upload():
    tool_fn, _, mm = _make_send_file_tool()

    result = _run_tool(
        tool_fn,
        _arguments(content="x" * (11 * 1024 * 1024)),
        _request_context("导出数据"),
    )

    assert "过大" in result["error"]
    mm.upload_file.assert_not_called()


def test_upload_failure_returns_domain_error():
    tool_fn, _, mm = _make_send_file_tool()
    mm.upload_file.return_value = None

    result = _run_tool(tool_fn, _arguments(), _request_context("发个文件"))

    assert "上传失败" in result["error"]


def test_uploaded_file_is_sent_to_originating_thread():
    tool_fn, _, mm = _make_send_file_tool()

    _run_tool(tool_fn, _arguments(message="附言"), _request_context("导出给我"))

    mm.send_post.assert_called_once_with(
        "ch1",
        "附言",
        "post_001",
        None,
        ["file_id_abc"],
    )


@pytest.mark.asyncio
async def test_capability_context_is_isolated_between_concurrent_requests():
    first = CapabilityContext("trace-1", "user-1", "channel-1", "post-1", "导出 A")
    second = CapabilityContext("trace-2", "user-2", "channel-2", "post-2", "导出 B")

    async def observe(context: CapabilityContext):
        with bind_capability_context(context):
            await asyncio.sleep(0)
            return get_capability_context()

    observed = await asyncio.gather(observe(first), observe(second))

    assert observed == [first, second]
    assert get_capability_context() is None
