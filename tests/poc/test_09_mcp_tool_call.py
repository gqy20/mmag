"""
PoC #09: 验证 SDK 持久连接 + 外部 MCP (crawl-mcp) 工具调用

测试场景: 发送一条明确要求使用 crawl_single 抓取网页的消息，
验证:
  1. SDK 能正确调度到 mcp__crawl-mcp__* 工具
  2. can_use_tool 回调放行 MCP 工具
  3. 工具结果能正常返回给 LLM
  4. LLM 基于工具结果生成最终回复
"""

import asyncio
import sys
import time
from pathlib import Path

# 确保项目根目录在 path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from claude_agent_sdk import ClaudeSDKClient, create_sdk_mcp_server
from claude_agent_sdk.types import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

from mmag.config import config
from mmag.sdk_llm import _tool_permission_callback


async def main():
    print("=" * 60)
    print("PoC #09: SDK + 外部 MCP (crawl-mcp) 工具调用验证")
    print("=" * 60)

    # ── 1. 构建选项（只挂外部 MCP，不挂内置工具）──
    project_root = Path(__file__).resolve().parents[3]
    mcp_json_path = str(project_root / ".mcp.json")

    options = ClaudeAgentOptions(
        model=config.anthropic_model,
        max_turns=8,  # 允许多轮 tool call
        system_prompt=(
            "你是一个测试助手。用户让你抓取网页时，请使用 crawl_single 或 crawl_batch 等 crawl-mcp 工具。"
            "抓取后总结页面内容返回给用户。"
        ),
        permission_mode="default",
        mcp_servers={"external": mcp_json_path},  # 只挂外部 MCP
        allowed_tools=[],
        disallowed_tools=["Bash", "Write", "Edit"],
        can_use_tool=_tool_permission_callback,  # 使用生产环境的权限回调
        env={
            "ANTHROPIC_API_KEY": config.anthropic_api_key,
        },
        setting_sources=[],
        cwd=str(project_root),
    )

    # ── 2. 连接 ──
    client = ClaudeSDKClient(options=options)
    t0 = time.monotonic()
    print(f"\n[1/3] 连接 SDK Client...")
    await client.connect()
    print(f"       ✅ 已连接 ({time.monotonic()-t0:.2f}s)")

    # ── 3. 发送触发 MCP 的查询 ──
    test_prompt = (
        "请用 crawl_single 工具抓取 https://httpbin.org/html 这个页面，"
        "然后告诉我页面标题和主要内容摘要。"
    )
    print(f"\n[2/3] 发送查询: {test_prompt[:80]}...")

    t1 = time.monotonic()
    await client.query(test_prompt)

    text_parts = []
    tool_calls = []
    cost = 0.0

    async for msg in client.receive_response():
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    text_parts.append(block.text)
                    print(f"       📝 Text: {block.text[:120]}...")
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(block.name)
                    input_str = str(block.input)[:150]
                    print(f"       🔧 Tool: {block.name}({input_str})")
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                print(f"       ❌ ResultMessage.is_error: {getattr(msg, 'errors', None)}")
            cost = getattr(msg, "total_cost_usd", 0) or 0.0
            usage = getattr(msg, "usage", None)
            if usage:
                print(f"       📊 tokens: in={usage.get('input_tokens',0)} out={usage.get('output_tokens',0)}")

    elapsed = time.monotonic() - t1
    full_text = "\n".join(text_parts)

    print(f"\n[3/3] 查询完成 ({elapsed:.2f}s)")
    print(f"       工具调用次数: {len(tool_calls)}")
    print(f"       调用的工具: {tool_calls}")
    print(f"       成本: ${cost:.4f}")
    print(f"\n{'='*60}")
    print("LLM 最终回复:")
    print("-" * 60)
    print(full_text or "(空)")
    print("=" * 60)

    # ── 验证结论 ──
    print("\n📋 验证结论:")
    any_mcp = any("mcp__" in t for t in tool_calls)
    any_crawl = any("crawl" in t.lower() for t in tool_calls)

    if any_crawl and full_text:
        print("  ✅ PASS: crawl-mcp 工具被成功调用且 LLM 生成了回复")
    elif any_mcp and full_text:
        print("  ✅ PASS: MCP 工具被调用且 LLM 生成了回复")
    elif tool_calls and full_text:
        print(f"  ⚠️ PARTIAL: 有工具调用({tool_calls})但不是 crawl-mcp")
    elif full_text:
        print("  ⚠️ PARTIAL: LLM 回复了但没有调用工具(可能用已有知识回答)")
    else:
        print("  ❌ FAIL: 无工具调用且无回复文本")

    await client.disconnect()
    print("\n✅ 测试结束")


if __name__ == "__main__":
    asyncio.run(main())
