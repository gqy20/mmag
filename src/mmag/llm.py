"""
LLM 适配器 (Anthropic) — 支持单轮对话 + Agentic Tool Use 循环
"""

import asyncio
import json
import time
from typing import Any, Optional

from anthropic import Anthropic

from .config import config
from .logger import get_logger, trace
from .tools import ToolRegistry

log = get_logger(__name__)

# 默认最大工具调用轮次
DEFAULT_MAX_TOOL_ROUNDS = 10


class LLM:
    """Anthropic Claude 封装 — 支持单轮对话和 Agentic Tool Use"""

    def __init__(self):
        kwargs: dict[str, Any] = {"api_key": config.anthropic_api_key}
        if config.anthropic_base_url:
            kwargs["base_url"] = config.anthropic_base_url
        self.client = Anthropic(**kwargs)
        self.model = config.anthropic_model
        self.call_count = 0
        log.info("LLM 初始化完成 | 模型: %s | API: %s",
                 self.model, config.anthropic_base_url or "官方")

    # ---- 单轮对话（保持向后兼容）----

    async def chat(self, messages: list[dict], system: str = "",
                   max_tokens: int = 1024) -> str:
        """普通对话（无工具）

        注意: StepFun 等兼容 API 可能返回 ThinkingBlock (思考过程)，
        需要过滤掉，只取 TextBlock 的文本内容。
        """
        self.call_count += 1
        t0 = time.monotonic()
        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            response = await asyncio.to_thread(self.client.messages.create, **kwargs)
            texts = _extract_text_blocks(response)
            elapsed = time.monotonic() - t0
            log.debug("%s LLM 单轮调用 (%.3fs, %d 字符输出)",
                       trace.prefix(), elapsed, len(texts))
            return texts if texts else "(模型返回为空)"
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("%s LLM 调用失败 (%.3fs): %s", trace.prefix(), elapsed, e)
            return f"⚠️ LLM 服务暂时不可用: {e}"

    async def chat_with_system(self, system_prompt: str, user_message: str,
                                max_tokens: int = 1024) -> str:
        """带系统提示词的对话快捷方法"""
        return await self.chat(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            max_tokens=max_tokens,
        )

    # ---- Agentic Tool Use 循环 ----

    async def agent_loop(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[Any] | None = None,
        tool_registry: ToolRegistry | None = None,
        max_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        max_tokens: int = 4096,
    ) -> str:
        """多轮 Agentic 循环：LLM 自主决定是否调用工具，直到产出最终回复。

        Args:
            messages: 对话历史消息列表
            system: 系统提示词
            tools: 工具定义列表（Anthropic API 格式），如果为空则降级为纯文本模式
            tool_registry: 工具注册表（用于执行工具）
            max_rounds: 最大工具调用轮数（安全阀，防止无限循环）
            max_tokens: 每轮 LLM 最大 token 数

        Returns:
            最终文本回复
        """
        # 无工具时降级为普通聊天
        if not tools or not tool_registry:
            log.debug("%s agent_loop: 无工具配置，降级为普通聊天", trace.prefix())
            return await self.chat(messages, system, max_tokens)

        tools_schema = [t for t in tools if isinstance(t, dict)]
        working_messages = list(messages)  # 拷贝，避免污染原始消息
        loop_t0 = time.monotonic()

        log.info("%s Agent Loop 开始 | 消息数=%d 工具数=%d 最大轮次=%d",
                 trace.prefix(), len(working_messages), len(tools_schema), max_rounds)

        for round_i in range(1, max_rounds + 1):
            self.call_count += 1
            round_t0 = time.monotonic()

            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": working_messages,
                    "tools": tools_schema,
                }
                if system:
                    kwargs["system"] = system

                response = await asyncio.to_thread(
                    self.client.messages.create, **kwargs
                )
            except Exception as e:
                elapsed = time.monotonic() - round_t0
                log.error("%s Round %d/%d LLM 调用失败 (%.3fs): %s",
                          trace.prefix(), round_i, max_rounds, elapsed, e)
                return f"⚠️ LLM 服务暂时不可用: {e}"

            # ---- 解析响应 ----
            text_parts = []
            tool_calls = []

            for block in response.content:
                block_type = getattr(block, "type", None)

                if block_type == "thinking":
                    continue

                elif block_type == "text":
                    if hasattr(block, "text") and block.text.strip():
                        text_parts.append(block.text)

                elif block_type == "tool_use":
                    tc_input = {}
                    if hasattr(block, "input") and block.input:
                        tc_input = dict(block.input)
                    tool_calls.append({
                        "id": getattr(block, "id", f"toolu_{round_i}"),
                        "name": getattr(block, "name", ""),
                        "input": tc_input,
                    })

            round_elapsed = time.monotonic() - round_t0

            # ---- 判断是否需要继续循环 ----
            if not tool_calls:
                final_text = "\n".join(text_parts).strip()
                total_elapsed = time.monotonic() - loop_t0
                log.info(
                    "%s Round %d/%d → 纯文本回复 (%.3fs) | 总耗时 %.3fs | 输出 %d 字符",
                    trace.prefix(), round_i, max_rounds, round_elapsed,
                    total_elapsed, len(final_text),
                )
                return final_text if final_text else "(模型返回为空)"

            # ---- 有工具调用 → 执行 → 注入结果 → 继续下一轮 ----
            tool_names = [tc["name"] for tc in tool_calls]
            log.info(
                "%s Round %d/%d → %d 个工具调用 [%s] (%.3fs)",
                trace.prefix(), round_i, max_rounds, len(tool_calls),
                ", ".join(tool_names), round_elapsed,
            )

            # 把 LLM 这一轮的完整响应加入历史（含文本 + 工具调用意图）
            assistant_content = []
            if text_parts:
                assistant_content.append({"type": "text", "text": "\n".join(text_parts)})
            for tc in tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })
            working_messages.append({"role": "assistant", "content": assistant_content})

            # 逐个执行工具，收集结果
            for tc in tool_calls:
                result_str = await tool_registry.execute(tc["name"], tc["input"])

                working_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": result_str,
                    }],
                })

        # 达到最大轮次限制
        total_elapsed = time.monotonic() - loop_t0
        log.warning("%s Agent Loop 达到最大轮次 %d (总耗时 %.3fs)，强制结束",
                     trace.prefix(), max_rounds, total_elapsed)
        # 取最后一轮的文本部分作为最终回复
        last_texts = []
        for msg in reversed(working_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                            last_texts.append(c["text"])
                elif isinstance(content, str) and content.strip():
                    last_texts.append(content)
                break
        return "\n".join(reversed(last_texts)) if last_texts else "⚠️ 处理超时，请重试"


# ============================================================
# 内部辅助函数
# ============================================================


def _extract_text_blocks(response) -> str:
    """从 LLM 响应中提取所有 TextBlock 文本（跳过 ThinkingBlock 等）"""
    texts = []
    for block in response.content:
        if hasattr(block, "text") and getattr(block, "type", None) == "text":
            texts.append(block.text)
    return "\n".join(texts).strip()
