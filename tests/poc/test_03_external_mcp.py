"""PoC #3: 外部 .mcp.json 加载 — 验证 SDK 能直接用 mmag 的 MCP 配置

目标:
- 把 mmag 根目录 .mcp.json 路径传给 SDK options.mcp_servers
- 验证 SDK 自动启动 crawl-mcp (uvx crawl-mcp) 子进程
- 验证工具能列出 (mcp__crawl-mcp__search_text 等)
- 跑一个真实 query: 搜索 "Mattermost" 看能不能拿到结果

这代表 "零改造" 路径 — mmag 现有 .mcp.json 直接复用, 不需要重写工具.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from claude_agent_sdk import AssistantMessage, ResultMessage, UserMessage, query
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolResultBlock, ToolUseBlock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_JSON = PROJECT_ROOT / ".mcp.json"


async def run_poc():
    assert MCP_JSON.exists(), f"未找到 .mcp.json: {MCP_JSON}"
    print(f"[配置] {MCP_JSON}")
    print(f"[内容] {MCP_JSON.read_text()}")

    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]

    options = ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=3,
        system_prompt="你是一个简洁的助手, 优先用 mcp__crawl-mcp__* 工具。",
        permission_mode="bypassPermissions",
        mcp_servers=str(MCP_JSON),  # ← 直接传 .mcp.json 路径
        # 允许的 mcp 工具名 — 这里只测 search_text 兜底
        allowed_tools=["mcp__crawl-mcp__search_text"],
        env=env,
        # 设置超时, 因为外部 MCP 启动 + uvx 拉包可能慢
        extra_args={"strict-mcp-config": None},
    )

    prompt = "用 mcp__crawl-mcp__search_text 工具搜索 'Mattermost' 关键词, 然后告诉我搜到了几条结果。"

    print(f"\nprompt: {prompt}\n")
    t0 = time.monotonic()
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    text_parts: list[str] = []
    result: ResultMessage | None = None
    err: str | None = None

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append({"name": block.name, "input": block.input})
                        print(f"  [ToolUse] {block.name}({str(block.input)[:100]})")
            elif isinstance(msg, UserMessage):
                if isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            preview = ""
                            if isinstance(block.content, str):
                                preview = block.content[:120]
                            elif isinstance(block.content, list) and block.content:
                                preview = str(block.content[0])[:120]
                            tool_results.append({"is_error": block.is_error, "preview": preview})
                            print(f"  [ToolResult] err={block.is_error} | {preview[:100]}")
            elif isinstance(msg, ResultMessage):
                result = msg
                print(f"\n[Result] stop={msg.stop_reason} turns={msg.num_turns} "
                      f"cost=${msg.total_cost_usd or 0:.4f}")
                if msg.is_error:
                    err = f"ResultMessage is_error: {msg.errors}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        import traceback
        traceback.print_exc()

    elapsed = time.monotonic() - t0
    print(f"\n[耗时] {elapsed:.2f}s")
    print(f"[Tool calls] {len(tool_calls)} | [Tool results] {len(tool_results)}")
    print(f"[Final text] {''.join(text_parts)[:400]}")
    if err:
        print(f"\n[ERR] {err}")
    return tool_calls, tool_results, result, err


if __name__ == "__main__":
    tc, tr, res, err = asyncio.run(run_poc())
    # 判定: 即使没拿到结果 (网络/限流问题), 至少证明外部 MCP 工具被 SDK 列出 + 发起调用
    used_search = any(t["name"] == "mcp__crawl-mcp__search_text" for t in tc)
    if err and not tc:
        print("\n[Verdict] FAIL — 外部 MCP 没启动")
        sys.exit(1)
    if not used_search:
        print("\n[Verdict] FAIL — 模型没用 mcp__crawl-mcp__search_text 工具")
        sys.exit(1)
    print("\n[Verdict] PASS — 外部 .mcp.json 加载路径通")
    sys.exit(0)
