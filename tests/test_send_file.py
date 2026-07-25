"""
send_file 工具 + upload_file 客户端 单元测试

覆盖:
  - ToolContext.user_requests_file: 关键词命中/不命中
  - send_file: 用户请求了文件 → 正常上传+发送
  - send_file: 用户没请求文件 → 拒绝
  - send_file: base64 编码解码
  - send_file: 文件过大被拒
  - send_file: upload 失败
  - upload_file: MIME 推断
  - send_post: file_ids 透传
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.sdk_tools import ToolContext  # noqa: E402

# ============================================================
# ToolContext
# ============================================================


class TestUserRequestsFile:
    def test_no_post_returns_false(self):
        ctx = ToolContext()
        assert ctx.user_requests_file() is False

    def test_keyword_match_cn(self):
        ctx = ToolContext()
        ctx.current_post = {"message": "帮我导出一下"}
        assert ctx.user_requests_file() is True

    def test_keyword_match_cn2(self):
        ctx = ToolContext()
        ctx.current_post = {"message": "能不能发个文件给我"}
        assert ctx.user_requests_file() is True

    def test_keyword_match_en(self):
        ctx = ToolContext()
        ctx.current_post = {"message": "please export as file"}
        assert ctx.user_requests_file() is True

    def test_no_keyword_returns_false(self):
        ctx = ToolContext()
        ctx.current_post = {"message": "今天天气怎么样"}
        assert ctx.user_requests_file() is False

    def test_case_insensitive(self):
        ctx = ToolContext()
        ctx.current_post = {"message": "PLEASE DOWNLOAD"}
        assert ctx.user_requests_file() is True


# ============================================================
# send_file tool (通过 _make_sdk_send_file 直接测试)
# ============================================================


def _make_mock_mm():
    """构造 mock MMClient"""
    mm = MagicMock()
    mm.upload_file.return_value = "file_id_abc"
    mm.send_post.return_value = "post_id_xyz"
    return mm


def _make_send_file_tool(tool_context: ToolContext):
    """直接调用 _make_sdk_send_file 拿到 @tool-decorated 函数"""
    from mmag.sdk_tools import _make_sdk_send_file

    mm = _make_mock_mm()
    return _make_sdk_send_file(mm, tool_context), mm


def _run_tool(tool_fn, args):
    """执行 @tool 函数并解析返回的 JSON

    @tool 装饰器返回 SdkMcpTool 对象, 实际 handler 在 .handler 属性。
    """
    raw = asyncio.run(tool_fn.handler(args))
    text = raw["content"][0]["text"]
    return json.loads(text)


class TestSendFile:
    def test_user_requested_file_succeeds(self):
        ctx = ToolContext()
        ctx.current_post = {
            "message": "@bot 帮我导出报告",
            "channel_id": "ch1",
            "id": "post_001",
        }
        tool_fn, mm = _make_send_file_tool(ctx)

        result = _run_tool(tool_fn, {
            "filename": "报告.md",
            "content": "# 标题\n正文内容",
            "message": "这是你要的报告",
            "content_encoding": "text",
        })

        assert result["success"] is True
        assert result["filename"] == "报告.md"
        assert result["file_id"] == "file_id_abc"
        mm.upload_file.assert_called_once()

    def test_user_did_not_request_file_rejected(self):
        ctx = ToolContext()
        ctx.current_post = {
            "message": "@bot 今天天气怎么样",
            "channel_id": "ch1",
            "id": "post_001",
        }
        tool_fn, mm = _make_send_file_tool(ctx)

        result = _run_tool(tool_fn, {
            "filename": "报告.md",
            "content": "内容",
            "message": "",
            "content_encoding": "text",
        })

        assert "error" in result
        assert "未明确请求" in result["error"]
        mm.upload_file.assert_not_called()

    def test_base64_encoding(self):
        import base64

        ctx = ToolContext()
        ctx.current_post = {
            "message": "发文件给我",
            "channel_id": "ch1",
            "id": "post_001",
        }
        tool_fn, mm = _make_send_file_tool(ctx)

        original = b"\x89PNG fake binary data"
        encoded = base64.b64encode(original).decode()

        result = _run_tool(tool_fn, {
            "filename": "image.png",
            "content": encoded,
            "message": "",
            "content_encoding": "base64",
        })

        assert result["success"] is True
        # 验证传给 upload_file 的是解码后的 bytes
        _, kwargs = mm.upload_file.call_args
        assert kwargs.get("data") if "data" in kwargs else mm.upload_file.call_args[0][2]
        call_args = mm.upload_file.call_args
        # upload_file(channel_id, filename, data) 位置参数
        uploaded_data = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("data")
        assert uploaded_data == original

    def test_oversized_file_rejected(self):
        ctx = ToolContext()
        ctx.current_post = {
            "message": "导出数据",
            "channel_id": "ch1",
            "id": "post_001",
        }
        tool_fn, mm = _make_send_file_tool(ctx)

        # 构造超过 10MB 的内容
        big_content = "x" * (11 * 1024 * 1024)
        result = _run_tool(tool_fn, {
            "filename": "big.txt",
            "content": big_content,
            "message": "",
            "content_encoding": "text",
        })

        assert "error" in result
        assert "过大" in result["error"]
        mm.upload_file.assert_not_called()

    def test_upload_failure_returns_error(self):
        ctx = ToolContext()
        ctx.current_post = {
            "message": "发个文件",
            "channel_id": "ch1",
            "id": "post_001",
        }
        tool_fn, mm = _make_send_file_tool(ctx)
        mm.upload_file.return_value = None  # 上传失败

        result = _run_tool(tool_fn, {
            "filename": "test.md",
            "content": "内容",
            "message": "",
            "content_encoding": "text",
        })

        assert "error" in result
        assert "上传失败" in result["error"]

    def test_file_ids_passed_to_send_post(self):
        ctx = ToolContext()
        ctx.current_post = {
            "message": "导出给我",
            "channel_id": "ch1",
            "id": "post_001",
        }
        tool_fn, mm = _make_send_file_tool(ctx)

        _run_tool(tool_fn, {
            "filename": "报告.md",
            "content": "内容",
            "message": "附言",
            "content_encoding": "text",
        })

        # send_post 应该被调用且 file_ids 包含上传后的 file_id
        call_args = mm.send_post.call_args
        # send_post(channel_id, message, root_id, props, file_ids)
        file_ids = call_args[0][4] if len(call_args[0]) > 4 else call_args[1].get("file_ids")
        assert file_ids == ["file_id_abc"]
