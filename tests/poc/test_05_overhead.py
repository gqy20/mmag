"""PoC #5: SDK 启动开销分析

Path B 比 Path A 慢 7.5x, 验证:
- 冷启动 vs 热启动 (同样 query 跑 3 次, 看是否递减)
- 无工具 SDK 启动 (排除 MCP 启动开销)
- 只调用一次 get_weather (排除工具执行开销)
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from claude_agent_sdk import AssistantMessage, ResultMessage, query
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock


def env_opts(model_only: bool = False) -> ClaudeAgentOptions:
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]
    return ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=1,
        system_prompt="用一句话回答。",
        permission_mode="bypassPermissions",
        env=env,
    )


async def time_one(label: str, prompt: str = "1+1=?", options: ClaudeAgentOptions | None = None):
    opts = options or env_opts()
    t0 = time.monotonic()
    text = ""
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text:
                        text = b.text
    except Exception as e:
        text = f"ERR: {e}"
    elapsed = time.monotonic() - t0
    print(f"  [{label:30s}] {elapsed:6.2f}s  {text[:60]}")
    return elapsed


async def main():
    print("=" * 70)
    print("测试 1: 连续 3 次同样 query, 看是否热启动")
    print("=" * 70)
    await time_one("call-1 (cold)", "1+1=?")
    await time_one("call-2 (warm)", "1+1=?")
    await time_one("call-3 (warm)", "1+1=?")

    print("\n" + "=" * 70)
    print("测试 2: 5 次同样的 query, 观察趋势")
    print("=" * 70)
    times = []
    for i in range(5):
        t = await time_one(f"warm-{i+1}", "1+1=?")
        times.append(t)
    print(f"  min: {min(times):.2f}s  median: {sorted(times)[len(times)//2]:.2f}s  max: {max(times):.2f}s")

    print("\n" + "=" * 70)
    print("测试 3: max_turns=1 (最短) vs max_turns=3 (可能拉长)")
    print("=" * 70)
    await time_one("max_turns=1", "1+1=?")
    opts2 = env_opts()
    opts2.max_turns = 3
    await time_one("max_turns=3", "1+1=?")


if __name__ == "__main__":
    asyncio.run(main())
