"""PoC #2: SDK in-process MCP server — 模拟 mmag 内置工具暴露

目标:
- 用 SDK 的 create_sdk_mcp_server 暴露 2 个伪 mmag 工具 (get_posts, search_knowledge)
- 验证 SDK 能列出工具 (mcp__mmag__get_posts)
- 验证 LLM 能主动调用并拿到结果
- 验证错误路径 (传错参数)

这是 "方案 A" 的核心可行性证明。
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
    ResultMessage,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolResultBlock, ToolUseBlock


# ============================================================================
# 模拟 mmag builtin.py 风格的 2 个工具
# ============================================================================

@tool(
    "get_posts",
    "获取频道最近的消息历史。用于回顾讨论内容、总结对话、查找特定信息。",
    {
        "channel_id": str,
        "limit": int,  # 可选
    },
)
async def get_posts(args):
    """模拟 mmag 的 get_posts 工具"""
    channel_id = args["channel_id"]
    limit = args.get("limit", 30)
    # 假装拉取了一些消息
    fake_posts = [
        {"id": f"post_{i}", "user": f"user_{i}", "message": f"mock message #{i}"}
        for i in range(min(limit, 3))
    ]
    return {
        "content": [
            {
                "type": "text",
                "text": f"{{\"channel_id\": \"{channel_id}\", \"count\": {len(fake_posts)}, "
                        f"\"posts\": {fake_posts}}}",
            }
        ]
    }


@tool(
    "search_knowledge",
    "搜索团队知识库中的信息。",
    {
        "channel_id": str,
        "query": str,
    },
)
async def search_knowledge(args):
    """模拟 mmag 的 search_knowledge 工具"""
    return {
        "content": [
            {
                "type": "text",
                "text": f"[mock] 知识库搜索: query='{args['query']}' channel={args['channel_id']} -> 0 条结果",
            }
        ]
    }


# ============================================================================
# PoC 跑
# ============================================================================


async def run_poc():
    # 1) 构造 SDK MCP server (在内存里, 跟 mmag builtin tools 风格一致)
    mmag_server = create_sdk_mcp_server(
        name="mmag",
        version="0.1.0",
        tools=[get_posts, search_knowledge],
    )

    # 2) 构造 options
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = os.environ["ANTHROPIC_BASE_URL"]

    options = ClaudeAgentOptions(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_turns=5,  # 给足 tool 调用轮次
        system_prompt=(
            "你是一个 mmag (Mattermost AI Agent) 助手。"
            "你可以使用 mcp__mmag__get_posts 和 mcp__mmag__search_knowledge 工具查询消息和知识库。"
            "当用户问'最近聊了什么'时, 用 get_posts 拉消息。"
        ),
        permission_mode="default",
        mcp_servers={"mmag": mmag_server},
        allowed_tools=[
            "mcp__mmag__get_posts",
            "mcp__mmag__search_knowledge",
        ],
        env=env,
    )

    # 3) 让模型必须用工具 (强制触发 tool call 路径)
    prompt = (
        "请用 get_posts 工具拉取 channel_id='test_chan_123' 的最近 5 条消息，"
        "然后告诉我拉到了几条。"
    )

    # 4) 跑
    print(f"prompt: {prompt}\n")
    t0 = time.monotonic()
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    text_parts: list[str] = []
    result: ResultMessage | None = None

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(
                            {
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            }
                        )
                        print(f"  [ToolUse] {block.name}({block.input})")
                    elif isinstance(block, ToolResultBlock):
                        content_preview = ""
                        if isinstance(block.content, str):
                            content_preview = block.content[:100]
                        elif isinstance(block.content, list) and block.content:
                            content_preview = str(block.content[0])[:100]
                        tool_results.append(
                            {
                                "tool_use_id": block.tool_use_id,
                                "is_error": block.is_error,
                                "preview": content_preview,
                            }
                        )
                        print(f"  [ToolResult] error={block.is_error} | {content_preview[:80]}")
            elif isinstance(msg, UserMessage):
                # 工具结果可能出现在 UserMessage 里
                if isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            content_preview = ""
                            if isinstance(block.content, str):
                                content_preview = block.content[:100]
                            elif isinstance(block.content, list) and block.content:
                                content_preview = str(block.content[0])[:100]
                            tool_results.append(
                                {
                                    "tool_use_id": block.tool_use_id,
                                    "is_error": block.is_error,
                                    "preview": content_preview,
                                }
                            )
                            print(f"  [ToolResult/UserMsg] error={block.is_error} | {content_preview[:80]}")
            elif isinstance(msg, ResultMessage):
                result = msg
                print(f"\n[Result] stop={msg.stop_reason} turns={msg.num_turns} "
                      f"cost=${msg.total_cost_usd or 0:.4f}")
                if msg.is_error:
                    print(f"[Result is_error] {msg.errors}")
    except Exception as e:
        print(f"\n[EXC] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

    elapsed = time.monotonic() - t0
    print(f"\n[耗时] {elapsed:.2f}s")
    print(f"[Tool calls] {len(tool_calls)} | [Tool results] {len(tool_results)}")
    print(f"[Final text] {''.join(text_parts)[:300]}")

    # 5) 断言
    # 判定核心: 工具真的被调了 + 工具结果回来了 (不论在 Assistant 还是 User message 里) + 模型据此回答
    success = (
        len(tool_calls) >= 1
        and any(tc["name"] == "mcp__mmag__get_posts" for tc in tool_calls)
        and len(tool_results) >= 1
        and any(not tr["is_error"] for tr in tool_results)
        and result is not None
        and not result.is_error
    )
    print(f"\n[Verdict] {'PASS' if success else 'FAIL'}")
    print(f"  - tool_calls: {len(tool_calls)}")
    print(f"  - tool_results: {len(tool_results)}")
    print(f"  - result: {'OK' if result and not result.is_error else 'FAIL'}")
    return success


if __name__ == "__main__":
    ok = asyncio.run(run_poc())
    sys.exit(0 if ok else 1)
