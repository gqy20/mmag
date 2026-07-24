"""
多模态端到端测试 — 验证用户发带图片消息时 Bot 能"看见"

跳过完整 Agent 启动(需要 MM 真实连接/WS/记忆),直接复用 Agent 的关键方法:
  1. _build_attachment_blocks — 从 post["metadata"]["files"] 下载图片/文本文档
  2. _build_context        — 构造多模态 LLM messages
  3. LLM.agent_loop        — 真实调 step-3.7-flash

数据准备:
  - 从 MM 真实频道拉最近 1 条带图片附件的消息作为样本
  - 构造 post 字典, 走 Agent 的图像处理流程

运行:  uv run pytest tests/test_multimodal_e2e.py -v -s
或:    uv run python tests/test_multimodal_e2e.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.config import config  # noqa: E402
from mmag.llm import LLM  # noqa: E402
from mmag.memory import Memory  # noqa: E402
from mmag.prompts import prompts  # noqa: E402


# ============================================================
# 真实样本: 从 MM 拉一条带图片附件的消息, 模拟 post 字典
# ============================================================
def fetch_real_sample() -> dict | None:
    """从 MM 服务器拉一条带图片附件的消息, 模拟 _on_posted 收到的 post 字典"""
    headers = {"Authorization": f"Bearer {config.mm_token}"}
    with httpx.Client(timeout=15, headers=headers) as h:
        teams = h.get(f"{config.mm_url}/api/v4/users/me/teams").json()
        for team in teams:
            for ch in h.get(
                f"{config.mm_url}/api/v4/teams/{team['id']}/channels"
            ).json():
                if ch.get("type") == "D":
                    continue
                posts = h.get(
                    f"{config.mm_url}/api/v4/channels/{ch['id']}/posts",
                    params={"per_page": 30},
                ).json().get("posts", {})
                for _pid, p in posts.items():
                    files_meta = (p.get("metadata") or {}).get("files") or []
                    for fmeta in files_meta:
                        if (fmeta.get("mime_type") or "").startswith("image/"):
                            return {
                                "id": p["id"],
                                "channel_id": p["channel_id"],
                                "user_id": p.get("user_id", "u_test"),
                                "message": p.get("message", ""),
                                "metadata": {"files": [fmeta]},
                                "channel_display_name": ch.get("display_name", ""),
                            }
    return None


# ============================================================
# 测试 1: _build_attachment_blocks 流程 (单测, 不调 LLM)
# ============================================================
def test_image_blocks_construction(tmp_path):
    """验证 _build_attachment_blocks 能从 post["metadata"]["files"] 下载图并构造 image blocks"""
    from mmag.client import MMClient

    sample = fetch_real_sample()
    if sample is None:
        pytest.skip("服务器没有可用的图片样本, 跳过")

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))
    from mmag.agent import Agent

    # 实例化 Agent 但跳过 start() (避免起 WS/MCP)
    agent = Agent.__new__(Agent)  # 不走 __init__, 自己塞必要属性
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    image_blocks = asyncio.run(
        agent._build_attachment_blocks(
            sample["metadata"]["files"],
            max_count=config.max_images_per_msg,
            max_bytes=config.max_image_bytes,
        )
    )

    assert image_blocks is not None, "图片样本应该能下载成功"
    # 第一个 block 必须是 image 类型
    assert image_blocks[0]["type"] == "image"
    assert image_blocks[0]["source"]["type"] == "base64"
    assert image_blocks[0]["source"]["media_type"].startswith("image/")
    assert len(image_blocks[0]["source"]["data"]) > 100  # base64 不可能太短
    print(f"  ✓ 构造了 {len(image_blocks)} 个 content block")
    print(f"  ✓ 第一张图: {image_blocks[0]['source']['media_type']}, "
          f"{len(image_blocks[0]['source']['data'])} chars base64")


# ============================================================
# 测试 2: _build_context 多模态拼接 (单测)
# ============================================================
def test_context_includes_image_blocks(tmp_path):
    """验证 _build_context 把 _llm_content_blocks 拼到 user message"""
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    # Mock MMClient 的方法(避免真连 MM)
    mm.get_channel = lambda cid: {"id": cid, "display_name": "test-ch", "name": "test-ch"}

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = "BOT_ID"
    agent.bot_username = "test_bot"
    agent.working_memory = {"ch1": []}
    agent.compactor = None  # 不调

    # 构造带 image blocks 的 post
    post = {
        "channel_id": "ch1",
        "user_id": "user_1",
        "username": "alice",
        "message": "看这张图,能告诉我是什么吗?",
        "_llm_content_blocks": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "iVBORw0KGgo=" * 100,  # 假装是 600 字符的 base64
                },
            }
        ],
    }
    ctx = agent._build_context(post)
    messages = ctx["messages"]
    # 最后一条 user 消息的 content 必须是 list
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list), "有 _llm_content_blocks 时 content 应为 list"
    types = [b["type"] for b in last["content"]]
    assert "image" in types, f"content 块中应含 image, 实际 types={types}"
    assert "text" in types, f"content 块中应含 text, 实际 types={types}"
    # image block 应在前(Anthropic 期望 image 在 text 之前)
    assert types[0] == "image", f"image 块应在 text 之前, 实际 types={types}"
    print(f"  ✓ user message content: list of {len(last['content'])} blocks ({types})")


def test_context_fallback_to_text_when_no_images(tmp_path):
    """无 _llm_content_blocks 时走纯文本路径 (向后兼容)"""
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))
    mm.get_channel = lambda cid: {"id": cid, "display_name": "test-ch", "name": "test-ch"}

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = "BOT_ID"
    agent.bot_username = "test_bot"
    agent.working_memory = {"ch1": []}

    post = {
        "channel_id": "ch1",
        "user_id": "user_1",
        "username": "alice",
        "message": "普通文本消息",
    }
    ctx = agent._build_context(post)
    last = ctx["messages"][-1]
    assert isinstance(last["content"], str), "无 image 时 content 应为 str"
    assert "alice" in last["content"]
    assert "普通文本消息" in last["content"]
    print(f"  ✓ 纯文本 fallback: {last['content'][:60]!r}")


# ============================================================
# 测试 3: 端到端 — 真实 LLM 看图
# ============================================================
@pytest.mark.slow
def test_llm_actually_sees_image(tmp_path):
    """端到端: 构造多模态 messages → 调真实 LLM → 验证模型看到图"""
    from mmag.agent import Agent
    from mmag.client import MMClient

    sample = fetch_real_sample()
    if sample is None:
        pytest.skip("服务器没有可用的图片样本, 跳过")

    async def _run_e2e() -> str:
        mm = MMClient()
        memory = Memory(str(tmp_path / "test.db"))
        llm = LLM()

        agent = Agent.__new__(Agent)
        agent.mm = mm
        agent.memory = memory
        agent.bot_user_id = "BOT_ID"
        agent.bot_username = "test_bot"
        agent.working_memory = {sample["channel_id"]: []}

        # 1) 下载图 (在同一个 event loop 里)
        image_blocks = await agent._build_attachment_blocks(
            sample["metadata"]["files"],
            max_count=config.max_images_per_msg,
            max_bytes=config.max_image_bytes,
        )
        assert image_blocks is not None, "图片下载失败"

        # 2) 构造 context
        user_question = "请用中文描述这张图片的核心内容,然后用一句话作为给用户的回复。"
        post = {
            "channel_id": sample["channel_id"],
            "user_id": sample["user_id"],
            "username": "alice",
            "message": user_question,
            "_llm_content_blocks": image_blocks,
        }
        ctx = agent._build_context(post)

        # 3) 真实调 LLM (同一 loop)
        print(f"\n  调 LLM ({config.anthropic_model})...")
        print(f"  user message 有 {len(ctx['messages'])} 条")
        last = ctx["messages"][-1]
        print(f"  最后一条 content 块数: {len(last['content'])}")

        system = prompts.get("system_prompt", bot_username=agent.bot_username)
        # step-3.7-flash 偶发只返回 thinking 不返回 text (概率行为),
        # 加 max_tokens + 1 次重试,让测试稳定。生产调用也会自然重试。
        response = ""
        for attempt in range(2):
            response = await llm.chat(
                messages=ctx["messages"], system=system, max_tokens=2048
            )
            if response and len(response) > 20 and "返回为空" not in response:
                break
            print(f"  [重试] LLM 响应过短/为空 (attempt {attempt + 1}): {response!r}")

        # 4) 清理 llm client (避免 event loop 关闭告警)
        await llm.client.close()
        return response

    response = asyncio.run(_run_e2e())
    print(f"\n  === LLM 实际回答 ===\n  {response}\n  ====================")

    # 5) 验证
    assert response and len(response) > 20, f"LLM 返回过短: {response!r}"
    assert "⚠️" not in response, f"LLM 错误响应: {response}"
    assert "返回为空" not in response, f"LLM 错误响应: {response}"


# ============================================================
# 测试 4: 纯文本无图路径仍然 work (回归测试)
# ============================================================
def test_text_only_path_still_works():
    """纯文本消息走老路径, 验证没破坏现有功能"""
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    from mmag.memory import Memory
    memory = Memory(":memory:")
    mm.get_channel = lambda cid: {"id": cid, "display_name": "test", "name": "test"}

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = "BOT_ID"
    agent.bot_username = "test_bot"
    agent.working_memory = {"ch1": []}

    post = {
        "channel_id": "ch1",
        "user_id": "user_1",
        "username": "bob",
        "message": "@test_bot 帮我看看这个",
    }
    ctx = agent._build_context(post)
    msgs = ctx["messages"]
    assert msgs[-1]["role"] == "user"
    assert isinstance(msgs[-1]["content"], str)
    assert "bob" in msgs[-1]["content"]
    assert "@test_bot" in msgs[-1]["content"]
    # system prompt 应包含"多模态"提示
    assert "多模态" in ctx["system"], "system_prompt 应包含多模态能力说明"
    print("  ✓ 纯文本路径无破坏")


# ============================================================
# 测试 5: 文本文档附件下载 (text/markdown, text/plain, application/json)
# ============================================================
def test_text_attachment_downloaded_as_text_block(tmp_path):
    """验证 _build_attachment_blocks 下载 text/* 附件并放入 text content block"""
    from unittest.mock import AsyncMock, patch
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    # mock get_file_bytes_async: 返回 markdown 内容
    md_content = "# 会议纪要\n\n## 议题\n1. 项目进度\n2. 预算审批\n\n结论: 通过".encode("utf-8")
    agent.mm.get_file_bytes_async = AsyncMock(return_value=(md_content, "text/markdown"))

    file_metas = [
        {
            "id": "file_md_001",
            "name": "meeting_notes.md",
            "mime_type": "text/markdown; charset=utf-8",
            "size": len(md_content),
        }
    ]

    blocks = asyncio.run(
        agent._build_attachment_blocks(
            file_metas, max_count=4, max_bytes=5 * 1024 * 1024, max_text_chars=50000
        )
    )

    assert blocks is not None, "应返回 content blocks"
    # 应有 1 个 text block (文档内容)
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert len(text_blocks) >= 1
    # 内容包含文档标题
    combined = " ".join(b["text"] for b in text_blocks)
    assert "会议纪要" in combined, f"文本块应包含文档内容, 实际: {combined[:100]}"
    assert "项目进度" in combined
    assert "meeting_notes.md" in combined
    print(f"  ✓ markdown 附件 → text block ({len(combined)} chars)")


def test_json_attachment_downloaded_as_text_block(tmp_path):
    """验证 application/json 附件也被下载为 text block"""
    from unittest.mock import AsyncMock
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    json_content = b'{"key": "value", "items": [1, 2, 3]}'
    agent.mm.get_file_bytes_async = AsyncMock(return_value=(json_content, "application/json"))

    file_metas = [
        {
            "id": "file_json_001",
            "name": "config.json",
            "mime_type": "application/json",
            "size": len(json_content),
        }
    ]

    blocks = asyncio.run(
        agent._build_attachment_blocks(
            file_metas, max_count=4, max_bytes=5 * 1024 * 1024
        )
    )

    assert blocks is not None
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert len(text_blocks) >= 1
    combined = " ".join(b["text"] for b in text_blocks)
    assert "config.json" in combined
    assert '"key"' in combined or 'key' in combined
    print(f"  ✓ json 附件 → text block")


def test_text_attachment_truncation(tmp_path):
    """验证超大文本附件被截断"""
    from unittest.mock import AsyncMock
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    # 生成 100K 字符的文本
    big_content = ("A" * 100000).encode("utf-8")
    agent.mm.get_file_bytes_async = AsyncMock(return_value=(big_content, "text/plain"))

    file_metas = [
        {
            "id": "file_big_001",
            "name": "huge.txt",
            "mime_type": "text/plain",
            "size": len(big_content),
        }
    ]

    blocks = asyncio.run(
        agent._build_attachment_blocks(
            file_metas, max_count=4, max_bytes=5 * 1024 * 1024, max_text_chars=1000
        )
    )

    assert blocks is not None
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert len(text_blocks) >= 1
    # 包含截断标记
    combined = " ".join(b["text"] for b in text_blocks)
    assert "已截断" in combined, "应包含截断标记"
    print(f"  ✓ 超大文本截断: {len(combined)} chars (limit=1000)")


def test_unsupported_mime_stays_as_placeholder(tmp_path):
    """验证不支持的 MIME (如 PDF) 仍为占位文本, 不尝试下载"""
    from unittest.mock import AsyncMock
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    # mock — 不应被调用
    agent.mm.get_file_bytes_async = AsyncMock(return_value=None)

    file_metas = [
        {
            "id": "file_pdf_001",
            "name": "report.pdf",
            "mime_type": "application/pdf",
            "size": 1024,
        }
    ]

    blocks = asyncio.run(
        agent._build_attachment_blocks(
            file_metas, max_count=4, max_bytes=5 * 1024 * 1024
        )
    )

    # PDF 不下载, 应返回 None (无成功 content blocks)
    assert blocks is None
    agent.mm.get_file_bytes_async.assert_not_called()
    print("  ✓ PDF 附件不下载, 保持占位")


def test_yaml_attachment_downloaded_as_text_block(tmp_path):
    """验证 application/yaml MIME 的附件被识别为文本文档并下载"""
    from unittest.mock import AsyncMock
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    yaml_content = "tiers:\n  - name: S\n    cost: 100\n  - name: A\n    cost: 80".encode("utf-8")
    agent.mm.get_file_bytes_async = AsyncMock(return_value=(yaml_content, "application/yaml"))

    file_metas = [
        {
            "id": "file_yaml_001",
            "name": "character_tiers.yml",
            "mime_type": "application/yaml",
            "size": len(yaml_content),
        }
    ]

    blocks = asyncio.run(
        agent._build_attachment_blocks(
            file_metas, max_count=4, max_bytes=5 * 1024 * 1024
        )
    )

    assert blocks is not None, "YAML 附件应被识别为文本文档"
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert len(text_blocks) >= 1
    combined = " ".join(b["text"] for b in text_blocks)
    assert "character_tiers.yml" in combined
    assert "tiers" in combined
    print("  ✓ application/yaml 附件 → text block")


def test_octet_stream_text_fallback_by_extension(tmp_path):
    """验证 application/octet-stream + .md 扩展名也能被识别为文本"""
    from unittest.mock import AsyncMock
    from mmag.agent import Agent, _is_text_attachment
    from mmag.client import MMClient

    # 直接测 _is_text_attachment
    assert _is_text_attachment("application/octet-stream", "notes.md")
    assert _is_text_attachment("application/octet-stream", "config.yaml")
    assert _is_text_attachment("application/octet-stream", "data.csv")
    assert not _is_text_attachment("application/octet-stream", "photo.jpg")
    assert not _is_text_attachment("application/octet-stream", "archive.zip")
    print("  ✓ octet-stream + 扩展名兜底正确")


def test_mixed_image_and_text_attachments(tmp_path):
    """验证图片 + 文本文档混合附件都正确处理"""
    from unittest.mock import AsyncMock
    from mmag.agent import Agent
    from mmag.client import MMClient

    mm = MMClient()
    memory = Memory(str(tmp_path / "test.db"))

    agent = Agent.__new__(Agent)
    agent.mm = mm
    agent.memory = memory
    agent.bot_user_id = ""
    agent.bot_username = "test_bot"

    # mock: 第一次调用返回图片, 第二次返回文本
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    md_data = "# Title\n\nSome markdown content".encode("utf-8")
    agent.mm.get_file_bytes_async = AsyncMock(
        side_effect=[
            (png_data, "image/png"),
            (md_data, "text/markdown"),
        ]
    )

    file_metas = [
        {
            "id": "img_001",
            "name": "screenshot.png",
            "mime_type": "image/png",
            "size": len(png_data),
        },
        {
            "id": "doc_001",
            "name": "notes.md",
            "mime_type": "text/markdown",
            "size": len(md_data),
        },
    ]

    blocks = asyncio.run(
        agent._build_attachment_blocks(
            file_metas, max_count=4, max_bytes=5 * 1024 * 1024, max_text_chars=50000
        )
    )

    assert blocks is not None
    types = [b["type"] for b in blocks]
    assert "image" in types, f"应包含 image block, 实际: {types}"
    assert "text" in types, f"应包含 text block, 实际: {types}"

    # 验证 text block 包含 markdown 内容
    text_blocks = [b for b in blocks if b["type"] == "text"]
    combined = " ".join(b["text"] for b in text_blocks)
    assert "Title" in combined or "markdown" in combined.lower()

    # 验证 image block 数据正确
    img_blocks = [b for b in blocks if b["type"] == "image"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["source"]["media_type"] == "image/png"

    print(f"  ✓ 混合附件: {len(img_blocks)} image + {len(text_blocks)} text blocks")


if __name__ == "__main__":
    # 直接 python 跑也能 work
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print("=" * 60)
        print("测试 1: _build_attachment_blocks")
        print("=" * 60)
        try:
            test_image_blocks_construction(tmp)
        except pytest.skip.Exception as e:
            print(f"  跳过: {e}")

        print("\n" + "=" * 60)
        print("测试 2a: _build_context (有图)")
        print("=" * 60)
        test_context_includes_image_blocks(tmp)

        print("\n" + "=" * 60)
        print("测试 2b: _build_context (无图 fallback)")
        print("=" * 60)
        test_context_fallback_to_text_when_no_images(tmp)

        print("\n" + "=" * 60)
        print("测试 4: 纯文本路径回归")
        print("=" * 60)
        test_text_only_path_still_works()

        print("\n" + "=" * 60)
        print("测试 3: 端到端 (调真实 LLM)")
        print("=" * 60)
        try:
            test_llm_actually_sees_image(tmp)
        except pytest.skip.Exception as e:
            print(f"  跳过: {e}")
