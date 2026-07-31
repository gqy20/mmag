"""
SDK LLM adapter 单元测试 — 验证 _build_content_blocks 保留 image blocks

覆盖:
  - 纯文本消息: 与旧 _build_prompt 行为一致 (text blocks 带 role 标签)
  - 多模态消息: image blocks 原样保留, 不展平为文本占位符
  - 混合消息: 多条消息 (user/assistant 交替) 正确拼接
  - system prompt: 作为首个 text block 注入
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mmag import sdk_llm  # noqa: E402
from mmag.sdk_llm import SDKLLM  # noqa: E402


@pytest.fixture
def sdk():
    """不调 start(), 只测纯函数方法"""
    return SDKLLM()


# ============================================================
# 纯文本路径 (向后兼容)
# ============================================================


class TestBuildContentBlocksTextOnly:
    def test_single_user_message(self, sdk):
        msgs = [{"role": "user", "content": "hello"}]
        blocks = sdk._build_content_blocks(msgs)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert "[User]" in blocks[0]["text"]
        assert "hello" in blocks[0]["text"]

    def test_multi_turn_conversation(self, sdk):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你的？"},
            {"role": "user", "content": "今天天气怎么样"},
        ]
        blocks = sdk._build_content_blocks(msgs)
        assert len(blocks) == 3
        assert "[User]" in blocks[0]["text"]
        assert "[Assistant]" in blocks[1]["text"]
        assert "[User]" in blocks[2]["text"]
        assert "天气" in blocks[2]["text"]

    def test_system_prompt_injected_as_first_block(self, sdk):
        msgs = [{"role": "user", "content": "hi"}]
        blocks = sdk._build_content_blocks(msgs, system="你是助手")
        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert "[System]" in blocks[0]["text"]
        assert "你是助手" in blocks[0]["text"]
        assert "[User]" in blocks[1]["text"]

    def test_empty_system_no_extra_block(self, sdk):
        msgs = [{"role": "user", "content": "hi"}]
        blocks = sdk._build_content_blocks(msgs, system="")
        assert len(blocks) == 1

    def test_empty_messages(self, sdk):
        blocks = sdk._build_content_blocks([], system="sys")
        assert len(blocks) == 1
        assert "[System]" in blocks[0]["text"]


# ============================================================
# 多模态路径 — image blocks 原样保留
# ============================================================


class TestBuildContentBlocksMultimodal:
    def _make_image_block(self, media_type="image/png", data="iVBORw0KGgo="):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }

    def test_image_block_preserved(self, sdk):
        """核心: image block 不被展平为文本占位符"""
        img = self._make_image_block()
        msgs = [
            {
                "role": "user",
                "content": [img, {"type": "text", "text": "看这张图"}],
            }
        ]
        blocks = sdk._build_content_blocks(msgs)
        # 应有 1 个 image block + 1 个 text block
        types = [b["type"] for b in blocks]
        assert "image" in types, f"image block 应保留, 实际 types={types}"
        assert "text" in types

    def test_image_block_data_unchanged(self, sdk):
        """image block 的 source.data 原样保留 (不截断/不替换)"""
        img = self._make_image_block(data="ABC123base64data==")
        msgs = [{"role": "user", "content": [img, {"type": "text", "text": "描述"}]}]
        blocks = sdk._build_content_blocks(msgs)
        img_blocks = [b for b in blocks if b["type"] == "image"]
        assert len(img_blocks) == 1
        assert img_blocks[0]["source"]["data"] == "ABC123base64data=="
        assert img_blocks[0]["source"]["media_type"] == "image/png"
        assert img_blocks[0]["source"]["type"] == "base64"

    def test_image_before_text_order(self, sdk):
        """image block 在 text block 之前 (Anthropic 期望的顺序)"""
        img = self._make_image_block()
        msgs = [
            {"role": "user", "content": [img, {"type": "text", "text": "这是什么?"}]}
        ]
        blocks = sdk._build_content_blocks(msgs)
        img_idx = next(i for i, b in enumerate(blocks) if b["type"] == "image")
        text_idx = next(i for i, b in enumerate(blocks) if b["type"] == "text")
        assert img_idx < text_idx, "image 应在 text 之前"

    def test_multiple_images_preserved(self, sdk):
        """多图场景: 所有 image blocks 保留"""
        img1 = self._make_image_block("image/png", "data1==")
        img2 = self._make_image_block("image/jpeg", "data2==")
        msgs = [
            {
                "role": "user",
                "content": [img1, img2, {"type": "text", "text": "这两张图"}],
            }
        ]
        blocks = sdk._build_content_blocks(msgs)
        img_blocks = [b for b in blocks if b["type"] == "image"]
        assert len(img_blocks) == 2
        assert img_blocks[0]["source"]["media_type"] == "image/png"
        assert img_blocks[1]["source"]["media_type"] == "image/jpeg"

    def test_mixed_turns_with_image(self, sdk):
        """user(图文) + assistant(纯文本) + user(纯文本) 混合"""
        img = self._make_image_block()
        msgs = [
            {"role": "user", "content": [img, {"type": "text", "text": "看图"}]},
            {"role": "assistant", "content": "这是一张截图"},
            {"role": "user", "content": "谢谢"},
        ]
        blocks = sdk._build_content_blocks(msgs)
        types = [b["type"] for b in blocks]
        assert types.count("image") == 1
        assert types.count("text") >= 3
        # image 在第一个 text 块之前 (属同一条 user 消息)
        img_idx = types.index("image")
        assert "[Assistant]" in blocks[img_idx + 2]["text"] or "[Assistant]" in blocks[img_idx + 1]["text"]

    def test_no_text_placeholder_for_image(self, sdk):
        """确认不再出现 [图片附件: ...] 文本占位符"""
        img = self._make_image_block()
        msgs = [{"role": "user", "content": [img, {"type": "text", "text": "看"}]}]
        blocks = sdk._build_content_blocks(msgs)
        for b in blocks:
            if b["type"] == "text":
                assert "图片附件" not in b["text"], "不应再出现图片占位符文本"


# ============================================================
# _message_stream 异步生成器
# ============================================================


class TestMessageStream:
    @pytest.mark.asyncio
    async def test_yields_single_message(self, sdk):
        """_message_stream 产出一条 stream-json 格式的 dict"""

        blocks = [{"type": "text", "text": "hello"}]
        stream = sdk._message_stream(blocks)
        results = []
        async for msg in stream:
            results.append(msg)
        assert len(results) == 1
        msg = results[0]
        assert msg["type"] == "user"
        assert msg["message"]["role"] == "user"
        assert msg["message"]["content"] is blocks  # 引用同一 list

    @pytest.mark.asyncio
    async def test_content_can_be_json_serialized(self, sdk):
        """产出的消息可以被 json.dumps 序列化 (SDK 的 query 会这样做)"""
        import json

        img = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "iVBORw0KGgo=",
            },
        }
        blocks = [img, {"type": "text", "text": "看图"}]
        stream = sdk._message_stream(blocks)
        async for msg in stream:
            s = json.dumps(msg)  # 不应 raise
            assert "image" in s
            assert "iVBORw0KGgo=" in s
            break


class TestPermissionPolicy:
    def test_path_with_shared_prefix_is_outside_project(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        sibling = tmp_path / "project-private"
        project.mkdir()
        sibling.mkdir()
        monkeypatch.setattr(sdk_llm, "_PROJECT_ROOT", str(project.resolve()))

        assert sdk_llm._is_path_allowed(str(project / "README.md")) is True
        assert sdk_llm._is_path_allowed(str(sibling / "secret.txt")) is False

    @pytest.mark.asyncio
    async def test_known_mmag_mcp_tool_is_allowed(self):
        decision = await sdk_llm._tool_permission_callback(
            "mcp__mmag__get_posts", {}, None
        )

        assert isinstance(decision, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_unknown_mcp_tool_is_denied(self):
        decision = await sdk_llm._tool_permission_callback(
            "mcp__untrusted__delete_all", {}, None
        )

        assert isinstance(decision, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_permission_callback_uses_runtime_visible_capability_names(self):
        visible = frozenset({"mcp__mmag__mcp_docs_search"})

        allowed = await sdk_llm._tool_permission_callback(
            "mcp__mmag__mcp_docs_search",
            {},
            None,
            allowed_mcp_tools=visible,
        )
        hidden = await sdk_llm._tool_permission_callback(
            "mcp__mmag__mcp_docs_delete",
            {},
            None,
            allowed_mcp_tools=visible,
        )

        assert isinstance(allowed, PermissionResultAllow)
        assert isinstance(hidden, PermissionResultDeny)
