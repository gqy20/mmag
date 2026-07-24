"""PoC #6: ClaudeSDKClient 持久连接 — 验证是否能消除 13-30s 启动开销

核心假设: 之前的 query() 每次都新建 CLI 子进程, 如果用 ClaudeSDKClient
保持连接, 后续 query() 应该只跑 LLM 调用, 没有 CLI 启动开销.

测试设计:
1. 创建一个 ClaudeSDKClient, 跑 connect()
2. 在同一 client 上连续 query 5 次
3. 记录每次的耗时, 验证是否 < 5s

如果验证通过 -> 整体替换 SDK 变得可行
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
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolResultBlock, ToolUseBlock


def build_options() -> ClaudeAgentOptions:
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]
    return ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=1,
        system_prompt="用一句话回答。",
        permission_mode="default",
        env=env,
    )


async def time_query(client: ClaudeSDKClient, label: str, prompt: str) -> tuple[float, str]:
    """单次 query + receive_response, 返回 (耗时秒, 文本)"""
    t0 = time.monotonic()
    text = ""
    tool_calls = 0
    turns = 0

    try:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text = block.text
                    elif isinstance(block, ToolUseBlock):
                        tool_calls += 1
            elif isinstance(msg, ResultMessage):
                turns = msg.num_turns or 0
    except Exception as e:
        text = f"ERR: {e}"
        import traceback
        traceback.print_exc()

    elapsed = time.monotonic() - t0
    print(f"  [{label:30s}] {elapsed:6.2f}s | turns={turns} tools={tool_calls} | {text[:60]}")
    return elapsed, text


async def main():
    print("=" * 70)
    print("测试 A: ClaudeSDKClient 持久连接 — 同一 client 跑 5 次")
    print("=" * 70)

    client = ClaudeSDKClient(options=build_options())

    # connect() 包含第一次子进程启动
    t0 = time.monotonic()
    await client.connect()
    connect_elapsed = time.monotonic() - t0
    print(f"  [connect (含 CLI 启动)] {connect_elapsed:6.2f}s")
    print()

    times = []
    for i in range(5):
        t, _ = await time_query(client, f"query-{i+1}", "1+1=?")
        times.append(t)

    await client.disconnect()

    print(f"\n  connect:    {connect_elapsed:.2f}s")
    print(f"  5 次 query 耗时: {times}")
    print(f"  min:        {min(times):.2f}s")
    print(f"  median:     {sorted(times)[len(times)//2]:.2f}s")
    print(f"  max:        {max(times):.2f}s")
    print(f"  avg:        {sum(times)/len(times):.2f}s")

    # 跟之前的 query() 一次性调用对比
    print("\n" + "=" * 70)
    print("对比")
    print("=" * 70)
    print(f"  query() 一次性:   13-30s 每次 (从 PoC #5)")
    print(f"  ClaudeSDKClient:  {min(times):.2f}s (min) / {sum(times)/len(times):.2f}s (avg)")

    if min(times) < 5.0:
        print("\n  [Verdict] PASS — 持久 client 大幅降低延迟, 替换可行")
    else:
        print(f"\n  [Verdict] MIXED — 仍然 {min(times):.1f}s 起, 需要继续优化")


if __name__ == "__main__":
    asyncio.run(main())
