"""PoC #1: SDK 烟雾测试 — 验证 query() 基本可用

目标:
- 确认 SDK 能连到配置的 base_url (step-3.7-flash) 或 claude 官方
- 拿到 AssistantMessage / ResultMessage
- 提取 stats (turns / tokens / cost)
- 不带任何工具, 只测 SDK 的异步事件流

如果 step-3.7-flash 走不通, 自动 fallback 到 claude-sonnet-4-6 (官方)
"""
import asyncio
import os
import sys
import time
from pathlib import Path

# 把项目根加进 path, 方便 import mmag.config
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    query,
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock

# ============================================================================
# 配置
# ============================================================================

PROMPT = "用一句话回答: 1+1=?"

ATTEMPT = 0


def build_options(model: str, base_url: str | None) -> ClaudeAgentOptions:
    """构造 options — 显式传 env, 让 SDK 用我们的 API key"""
    env: dict[str, str] = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return ClaudeAgentOptions(
        model=model,
        max_turns=1,
        system_prompt="你是一个简洁的助手, 回答限制在 30 字内。",
        permission_mode="bypassPermissions",
        env=env,
    )


async def run_once(model: str, base_url: str | None, label: str) -> bool:
    """跑一次 query, 返回是否成功"""
    print(f"\n{'=' * 60}")
    print(f"[{label}] model={model} base_url={base_url or 'default'}")
    print("=" * 60)

    options = build_options(model, base_url)
    t0 = time.monotonic()
    text_chunks: list[str] = []
    result_msg: ResultMessage | None = None
    error: str | None = None

    try:
        async for msg in query(prompt=PROMPT, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        text_chunks.append(block.text)
                        print(f"  [Assistant] {block.text[:100]}")
            elif isinstance(msg, ResultMessage):
                result_msg = msg
                print(f"  [Result] stop={msg.stop_reason} "
                      f"turns={msg.num_turns} "
                      f"cost=${(msg.total_cost_usd or 0):.4f} "
                      f"tokens={msg.usage}")
                if msg.is_error:
                    error = f"ResultMessage is_error: {msg.errors}"
    except CLINotFoundError as e:
        error = f"CLI 未找到: {e}"
    except CLIConnectionError as e:
        error = f"连接失败: {e}"
    except ProcessError as e:
        error = f"进程失败 (exit={e.exit_code}): {e.stderr or e}"
    except Exception as e:
        error = f"未知异常 ({type(e).__name__}): {e}"
        import traceback
        traceback.print_exc()

    elapsed = time.monotonic() - t0
    print(f"  [耗时] {elapsed:.2f}s")
    print(f"  [文本] {''.join(text_chunks)[:200]}")

    if error:
        print(f"  [FAIL] {error}")
        return False
    if not text_chunks and not (result_msg and result_msg.result):
        print("  [FAIL] 无文本输出")
        return False
    print("  [OK]")
    return True


async def main():
    """按优先级尝试 (1) step-3.7-flash 兼容接口, (2) 官方 claude-sonnet-4-6"""
    global ATTEMPT
    results = {}

    # 配置 1: 当前 mmag 用的 step-3.7-flash (验证兼容接口)
    if os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("ANTHROPIC_MODEL"):
        results["step-3.7-flash"] = await run_once(
            os.environ["ANTHROPIC_MODEL"],
            os.environ["ANTHROPIC_BASE_URL"],
            "stepfun",
        )

    # 配置 2: 官方 claude-sonnet-4-6 (验证 SDK 基线)
    results["claude-sonnet-4-6"] = await run_once(
        "claude-sonnet-4-6",
        None,
        "official",
    )

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for k, v in results.items():
        print(f"  {k:30s} {'OK' if v else 'FAIL'}")

    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
