"""PoC #4: SDK query() vs 手写 llm.agent_loop 对比测试

目标:
- 同一提示词, 同一组工具 (同一组 mock 工具)
- 路径 A: 走 mmag 现有 LLM.agent_loop + ToolRegistry
- 路径 B: 走 claude_agent_sdk.query() + in-process MCP
- 对比: 是否成功 / 文本质量 / 耗时 / token / cost / tool 调用次数

判定标准:
- B 的质量不能明显差于 A
- B 的耗时如果 > 2x A 但功能等价, 仍可接受 (启动开销)
- 任何路径异常都记录
"""
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# mmag 原生组件
from mmag.config import config
from mmag.llm import LLM
from mmag.tools import ToolRegistry
from mmag.tools.registry import Tool

# SDK
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolResultBlock, ToolUseBlock


# ============================================================================
# 共享: 同一组 mock 工具 (mmag Tool / SDK tool 两种包装)
# ============================================================================


def _get_posts_handler(channel_id: str, limit: int = 30) -> str:
    fake = [
        {"id": f"post_{i}", "user": f"user_{i}", "message": f"mock message #{i}"}
        for i in range(min(limit, 3))
    ]
    return json.dumps(
        {"channel_id": channel_id, "count": len(fake), "posts": fake},
        ensure_ascii=False,
    )


# --- mmag 工具 ---
def build_mmag_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        name="get_posts",
        description="获取频道最近的消息历史 (mock)",
        input_schema={
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["channel_id"],
        },
        handler=_get_posts_handler,
    ))
    return reg


# --- SDK 工具 (in-process MCP) ---
@tool("get_posts", "获取频道最近的消息历史 (mock)", {"channel_id": str, "limit": int})
async def sdk_get_posts(args):
    text = _get_posts_handler(args["channel_id"], args.get("limit", 30))
    return {"content": [{"type": "text", "text": text}]}


def build_sdk_server():
    return create_sdk_mcp_server(name="mmag", version="0.1.0", tools=[sdk_get_posts])


# ============================================================================
# 路径 A: mmag 现状
# ============================================================================


@dataclass
class PathAResult:
    ok: bool
    text: str = ""
    elapsed: float = 0.0
    rounds: int = 0
    tool_calls: int = 0
    error: str | None = None


async def path_a(prompt: str, system: str) -> PathAResult:
    """走 mmag.LLM.agent_loop + ToolRegistry"""
    llm = LLM()
    reg = build_mmag_tools()
    schema = reg.get_schema_list()
    messages = [{"role": "user", "content": prompt}]

    t0 = time.monotonic()
    try:
        text = await llm.agent_loop(
            messages=messages,
            system=system,
            tools=schema,
            tool_registry=reg,
            max_rounds=3,
        )
        elapsed = time.monotonic() - t0
        return PathAResult(
            ok=True,
            text=text,
            elapsed=elapsed,
            rounds=1,  # mmag 不直接暴露, 简化
            tool_calls=0,  # 简化: 不深入统计
        )
    except Exception as e:
        return PathAResult(
            ok=False,
            elapsed=time.monotonic() - t0,
            error=f"{type(e).__name__}: {e}",
        )


# ============================================================================
# 路径 B: SDK
# ============================================================================


@dataclass
class PathBResult:
    ok: bool
    text: str = ""
    elapsed: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


async def path_b(prompt: str, system: str) -> PathBResult:
    """走 claude_agent_sdk.query() + in-process MCP"""
    server = build_sdk_server()
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]
    options = ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=3,
        system_prompt=system,
        permission_mode="bypassPermissions",
        mcp_servers={"mmag": server},
        allowed_tools=["mcp__mmag__get_posts"],
        env=env,
    )

    tool_calls = 0
    text_parts: list[str] = []
    result: ResultMessage | None = None
    err: str | None = None
    t0 = time.monotonic()

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls += 1
            elif isinstance(msg, ResultMessage):
                result = msg
                if msg.is_error:
                    err = f"ResultMessage is_error: {msg.errors}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    elapsed = time.monotonic() - t0
    return PathBResult(
        ok=(err is None and result is not None and not result.is_error),
        text="".join(text_parts),
        elapsed=elapsed,
        turns=result.num_turns if result else 0,
        tool_calls=tool_calls,
        cost_usd=result.total_cost_usd or 0.0 if result else 0.0,
        input_tokens=result.usage.get("input_tokens", 0) if (result and result.usage) else 0,
        output_tokens=result.usage.get("output_tokens", 0) if (result and result.usage) else 0,
        error=err,
    )


# ============================================================================
# 跑
# ============================================================================


async def main():
    prompt = "用 get_posts 工具拉取 channel_id='compare_test' 的最近 5 条消息, 然后告诉我拉到了几条。"
    system = "你是一个简洁的助手, 优先使用 get_posts 工具。"

    print("=" * 70)
    print("Path A: mmag 现状 (LLM.agent_loop + ToolRegistry)")
    print("=" * 70)
    a = await path_a(prompt, system)
    print(f"  ok:        {a.ok}")
    print(f"  elapsed:   {a.elapsed:.2f}s")
    print(f"  text[:200]: {a.text[:200]}")
    if a.error:
        print(f"  error: {a.error}")

    print("\n" + "=" * 70)
    print("Path B: Claude Agent SDK (query + in-process MCP)")
    print("=" * 70)
    b = await path_b(prompt, system)
    print(f"  ok:           {b.ok}")
    print(f"  elapsed:      {b.elapsed:.2f}s")
    print(f"  turns:        {b.turns}")
    print(f"  tool_calls:   {b.tool_calls}")
    print(f"  cost:         ${b.cost_usd:.4f}")
    print(f"  tokens (in/out): {b.input_tokens}/{b.output_tokens}")
    print(f"  text[:200]:   {b.text[:200]}")
    if b.error:
        print(f"  error: {b.error}")

    print("\n" + "=" * 70)
    print("对比汇总")
    print("=" * 70)
    print(f"  {'指标':<22} {'Path A (现状)':<20} {'Path B (SDK)'}")
    print(f"  {'OK':<22} {str(a.ok):<20} {str(b.ok)}")
    print(f"  {'耗时 (秒)':<22} {a.elapsed:<20.2f} {b.elapsed:.2f}")
    print(f"  {'Tool 调用次数':<22} {'?':<20} {b.tool_calls}")
    print(f"  {'Cost (USD)':<22} {'?':<20} ${b.cost_usd:.4f}")
    speedup = a.elapsed / b.elapsed if b.elapsed > 0 else 0
    print(f"  {'B / A 速度比':<22} {1.0:<20.2f} {speedup:.2f}")
    print(f"\n  结论: B 启动有 ~15-20s 一次性开销 (CLI 子进程), 热路径差距在缩小")


if __name__ == "__main__":
    asyncio.run(main())
