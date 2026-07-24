"""PoC #8a: 串行 (独立进程, 避免跨 loop 问题)"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, create_sdk_mcp_server, tool
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock


@tool("get_posts", "获取消息 (mock)", {"channel_id": str, "limit": int})
async def sdk_get_posts(args):
    return {"content": [{"type": "text",
                          "text": f'{{"channel": "{args["channel_id"]}", "count": {args.get("limit", 3)}}}'}]}


def build_client() -> ClaudeSDKClient:
    server = create_sdk_mcp_server(name="mmag", version="0.1.0", tools=[sdk_get_posts])
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]
    return ClaudeSDKClient(options=ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=3,
        system_prompt="你是 mmag 助手。",
        permission_mode="default",
        mcp_servers={"mmag": server},
        allowed_tools=["mcp__mmag__get_posts"],
        env=env,
        setting_sources=[],
    ))


async def main():
    print("测试: 1 个 client 串行处理 5 条")
    client = build_client()
    t0 = time.monotonic()
    await client.connect()
    print(f"  [connect] {time.monotonic()-t0:.2f}s")

    times = []
    for i in range(5):
        t0 = time.monotonic()
        await client.query(f"用 get_posts 查 channel_id='chan_{i}' 的 3 条消息, 只报数字。")
        text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text:
                        text = b.text
        times.append(time.monotonic() - t0)
        print(f"  [msg-{i+1}] {times[-1]:.2f}s | {text[:50]}")

    total = sum(times)
    print(f"\n  [总耗时] {total:.2f}s (avg {total/5:.2f}s/条)")


if __name__ == "__main__":
    asyncio.run(main())
