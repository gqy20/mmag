"""PoC #8: 连接池架构验证 — N 个 ClaudeSDKClient 并发能力

关键问题: ClaudeSDKClient 是 stateful, 一次只能跑一个 conversation.
mmag 场景: 多频道可能同时来消息.

测试:
1. 串行 (1 client, 5 个 MM 消息顺序处理) — 应该 4-6s/次
2. 并发 (5 client 池, 5 个消息并发处理) — 应该 ~6-9s 总耗时

如果并发池总耗时 < 串行, 连接池就有价值.
否则, 1 个 client + 串行队列就够.
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
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolUseBlock


@tool("get_posts", "获取消息 (mock)", {"channel_id": str, "limit": int})
async def sdk_get_posts(args):
    return {"content": [{"type": "text",
                          "text": f'{{"channel": "{args["channel_id"]}", "count": {args.get("limit", 3)}}}'}]}


def build_client() -> ClaudeSDKClient:
    server = create_sdk_mcp_server(name="mmag", version="0.1.0", tools=[sdk_get_posts])
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]
    options = ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=3,
        system_prompt="你是 mmag 助手。",
        permission_mode="default",
        mcp_servers={"mmag": server},
        allowed_tools=["mcp__mmag__get_posts"],
        env=env,
        setting_sources=[],
    )
    return ClaudeSDKClient(options=options)


async def process_one(client: ClaudeSDKClient, prompt: str) -> tuple[float, str]:
    """用给定 client 处理一条消息, 返回 (耗时, 文本)"""
    t0 = time.monotonic()
    text = ""
    try:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text:
                        text = b.text
    except Exception as e:
        text = f"ERR: {e}"
    return time.monotonic() - t0, text


async def test_serial():
    """测试 1: 1 个 client 串行处理 5 条消息"""
    print("=" * 70)
    print("测试 1: 串行 (1 client 顺序处理 5 条)")
    print("=" * 70)

    client = build_client()
    t0 = time.monotonic()
    await client.connect()
    print(f"  [connect] {time.monotonic()-t0:.2f}s")

    prompts = [
        f"用 get_posts 查 channel_id='chan_{i}' 的 3 条消息, 报数字。" for i in range(5)
    ]

    t0 = time.monotonic()
    times = []
    for p in prompts:
        t, _ = await process_one(client, p)
        times.append(t)
        print(f"    [serial msg] {t:.2f}s")
    total = time.monotonic() - t0

    await client.disconnect()
    print(f"  [5 条总耗时] {total:.2f}s (avg {total/5:.2f}s/条)")
    return total


async def test_pool(n: int = 3):
    """测试 2: N 个 client 池并发处理 5 条消息"""
    print("\n" + "=" * 70)
    print(f"测试 2: 池 (N={n} client 并发处理 5 条)")
    print("=" * 70)

    # 创建 N 个 client
    clients = [build_client() for _ in range(n)]
    t0 = time.monotonic()
    await asyncio.gather(*[c.connect() for c in clients])
    print(f"  [connect ×{n}] {time.monotonic()-t0:.2f}s")

    prompts = [
        f"用 get_posts 查 channel_id='chan_{i}' 的 3 条消息, 报数字。" for i in range(5)
    ]

    # 简单 round-robin 分配
    t0 = time.monotonic()
    tasks = [process_one(clients[i % n], p) for i, p in enumerate(prompts)]
    results = await asyncio.gather(*tasks)
    total = time.monotonic() - t0

    for i, (t, txt) in enumerate(results):
        print(f"    [pool msg-{i+1}] {t:.2f}s | {txt[:50]}")

    await asyncio.gather(*[c.disconnect() for c in clients])
    print(f"  [5 条总耗时] {total:.2f}s (avg {total/5:.2f}s/条)")
    return total


async def main():
    serial_total = await test_serial()
    pool_total = await test_pool(n=3)

    print("\n" + "=" * 70)
    print("结论")
    print("=" * 70)
    speedup = serial_total / pool_total if pool_total > 0 else 0
    print(f"  串行 (1 client):  {serial_total:.2f}s")
    print(f"  并发 (3 client):  {pool_total:.2f}s")
    print(f"  加速比:           {speedup:.2f}x")
    if speedup > 1.5:
        print("  → 池化有价值 (3 client 池加速 > 1.5x)")
    elif speedup > 1.1:
        print("  → 池化略有价值 (加速 1.1-1.5x)")
    else:
        print("  → 池化无明显价值 (1 client 串行就够了)")


if __name__ == "__main__":
    asyncio.run(main())
