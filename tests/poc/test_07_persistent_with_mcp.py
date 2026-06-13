"""PoC #7: 持久 Client + MCP 工具 + setting_sources=[] 优化

要测的:
1. 持久 client 带 in-process MCP server — 验证 mmag 真实场景
2. setting_sources=[] 减少 SDK 注入的 system prompt
3. cost_usd 在持久 client 多次 query 后是否累计

这是 "持久 client + 工具" 的真实场景, 也是 mmag 改造后最像生产的样子.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolResultBlock, ToolUseBlock


@tool("get_posts", "获取频道最近消息 (mock)", {"channel_id": str, "limit": int})
async def sdk_get_posts(args):
    fake = [{"id": f"p_{i}", "msg": f"hello {i}"} for i in range(min(args.get("limit", 3), 3))]
    return {"content": [{"type": "text", "text": f'{{"count": {len(fake)}, "posts": {fake}}}'}]}


async def test_a_persistent_with_mcp():
    """场景 A: 持久 client + in-process MCP, 模拟 mmag 真实用法"""
    print("=" * 70)
    print("场景 A: 持久 client + in-process MCP, 5 次混合 query")
    print("=" * 70)

    server = create_sdk_mcp_server(name="mmag", version="0.1.0", tools=[sdk_get_posts])
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]

    options = ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=3,
        system_prompt="你是 mmag 助手, 可以用 mcp__mmag__get_posts 查消息。回答简洁。",
        permission_mode="bypassPermissions",
        mcp_servers={"mmag": server},
        allowed_tools=["mcp__mmag__get_posts"],
        env=env,
        # ← 关键: setting_sources=[] 不加载项目 CLAUDE.md
        setting_sources=[],
    )

    client = ClaudeSDKClient(options=options)
    t0 = time.monotonic()
    await client.connect()
    print(f"  [connect] {time.monotonic()-t0:.2f}s")
    print()

    times = []
    prompts = [
        "1+1=?",  # 纯文本
        "用 get_posts 查 channel_id='chan_a' 的 5 条消息, 报数字。",  # tool
        "今天天气?  (没有工具, 靠模型编)",  # 纯文本
        "用 get_posts 查 channel_id='chan_b' 的 2 条消息, 报数字。",  # tool
        "1+1=?",  # 纯文本
    ]
    for i, prompt in enumerate(prompts, 1):
        t0 = time.monotonic()
        text = ""
        tool_calls = 0
        cost = 0.0
        in_tok = out_tok = 0
        try:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text:
                            text = b.text
                        elif isinstance(b, ToolUseBlock):
                            tool_calls += 1
                elif isinstance(msg, ResultMessage):
                    cost = msg.total_cost_usd or 0.0
                    if msg.usage:
                        in_tok = msg.usage.get("input_tokens", 0)
                        out_tok = msg.usage.get("output_tokens", 0)
        except Exception as e:
            text = f"ERR: {e}"
        elapsed = time.monotonic() - t0
        times.append(elapsed)
        kind = "tool" if tool_calls else "text"
        print(f"  [q{i} {kind:4s}] {elapsed:6.2f}s | tools={tool_calls} "
              f"in_tok={in_tok:5d} cost=${cost:.4f} | {text[:50]}")

    await client.disconnect()
    print(f"\n  min={min(times):.2f}s  median={sorted(times)[2]:.2f}s  max={max(times):.2f}s")


async def test_b_setting_sources_comparison():
    """场景 B: 对比 setting_sources=[] vs 默认"""
    print("\n" + "=" * 70)
    print("场景 B: setting_sources=[] 减少 system prompt 注入")
    print("=" * 70)

    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]

    for label, sources in [("默认 (加载项目配置)", ["project"]), ("空 (setting_sources=[])", [])]:
        opts = ClaudeAgentOptions(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_turns=1,
            system_prompt="用一句话回答。",
            permission_mode="bypassPermissions",
            env=env,
            setting_sources=sources,
        )
        client = ClaudeSDKClient(options=opts)
        t0 = time.monotonic()
        await client.connect()
        connect_t = time.monotonic() - t0

        t0 = time.monotonic()
        text = ""
        in_tok = 0
        await client.query("1+1=?")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text:
                        text = b.text
            elif isinstance(msg, ResultMessage):
                if msg.usage:
                    in_tok = msg.usage.get("input_tokens", 0)
        query_t = time.monotonic() - t0

        print(f"  [{label:25s}] connect={connect_t:5.2f}s  query={query_t:5.2f}s  "
              f"in_tok={in_tok:6d}  | {text[:40]}")
        await client.disconnect()


async def main():
    await test_a_persistent_with_mcp()
    await test_b_setting_sources_comparison()


if __name__ == "__main__":
    asyncio.run(main())
