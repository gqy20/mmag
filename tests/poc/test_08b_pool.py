"""PoC #8b: 池 (独立进程, N 个 client 并发)"""
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
        permission_mode="bypassPermissions",
        mcp_servers={"mmag": server},
        allowed_tools=["mcp__mmag__get_posts"],
        env=env,
        setting_sources=[],
    ))


async def process_one(client: ClaudeSDKClient, prompt: str) -> tuple[float, str]:
    t0 = time.monotonic()
    text = ""
    await client.query(prompt)
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text:
                    text = b.text
    return time.monotonic() - t0, text


async def main():
    N = 3
    print(f"测试: N={N} 个 client 并发处理 5 条")
    clients = [build_client() for _ in range(N)]

    t0 = time.monotonic()
    await asyncio.gather(*[c.connect() for c in clients])
    print(f"  [connect ×{N}] {time.monotonic()-t0:.2f}s")

    prompts = [f"用 get_posts 查 channel_id='chan_{i}' 的 3 条消息, 只报数字。" for i in range(5)]

    t0 = time.monotonic()
    tasks = [process_one(clients[i % N], p) for i, p in enumerate(prompts)]
    results = await asyncio.gather(*tasks)
    total = time.monotonic() - t0

    for i, (t, txt) in enumerate(results):
        print(f"  [msg-{i+1} on client-{i%N}] {t:.2f}s | {txt[:50]}")
    print(f"\n  [5 条总耗时] {total:.2f}s (avg {total/5:.2f}s/条, peak ~{max(r[0] for r in results):.2f}s)")


if __name__ == "__main__":
    asyncio.run(main())
