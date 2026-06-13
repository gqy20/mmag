"""
SDK LLM Adapter — 封装 Claude Agent SDK 持久客户端，提供与 LLM 类完全一致的公共 API。

公共 API:
  - agent_loop(messages, system, tools, tool_registry, max_rounds) -> str
  - chat(messages, system, max_tokens) -> str
  - chat_with_system(system_prompt, user_message, max_tokens) -> str

生命周期:
  - start(tool_funcs, mcp_json_path) -> connect()
  - stop() -> disconnect()
  - reconnect() -> 断线后自动重建连接
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock, ToolUseBlock

from .config import config
from .logger import get_logger, trace

log = get_logger(__name__)


# ============================================================
# 异常
# ============================================================


class SDKLLMError(Exception):
    """SDK LLM 调用失败的领域异常 — 对标 LLMError"""


# ============================================================
# 国产模型训练痕迹过滤（从 llm.py 原样搬运）
# ============================================================

_TOOL_CALL_XML_PATTERN = r'<invoke\s+.*?\s*>[\s\S]*?</invoke>'
_RE_TOOL_CALL_XML = re.compile(_TOOL_CALL_XML_PATTERN)

_THINKING_PATTERN = r'<think[\s\S]*?</think\s*>'
_RE_THINKING = re.compile(_THINKING_PATTERN)


def _strip_tool_call_xml(text: str) -> str:
    """过滤 step-3.7-flash 等国产模型意外输出的 invoke XML 字符串"""
    if "<invoke" not in text:
        return text
    return _RE_TOOL_CALL_XML.sub("", text)


def _strip_thinking_tags(text: str) -> str:
    """过滤 step-3.7-flash 等模型把 thinking 过程作为普通 text 输出的部分"""
    if "<think" not in text:
        return text
    return _RE_THINKING.sub("", text)


def _strip_model_artifacts(text: str) -> str:
    """组合入口: 一次剥掉所有已知的国产模型训练痕迹输出"""
    return _strip_thinking_tags(_strip_tool_call_xml(text))


# ============================================================
# SDK LLM Adapter 类
# ============================================================


class SDKLLM:
    """Claude Agent SDK adapter — 持久客户端, agentic loop 由 SDK 内部处理。

    公共 API 与 LLM 类完全对齐:
      - agent_loop() → SDK query + receive_response (agentic)
      - chat()        → SDK query + receive_response (单轮/重试用)
      - chat_with_system() → chat 的快捷封装
    """

    def __init__(self):
        self.client: ClaudeSDKClient | None = None
        self._connected = False
        self._mcp_json_path: str | None = None
        self._saved_tool_funcs: list | None = None
        self.call_count = 0
        self.model = config.anthropic_model

    # ---- 生命周期 ----

    async def start(self, tool_funcs: list | None = None, mcp_json_path: str | None = None):
        """初始化并连接持久 SDK 客户端。在 Agent.start() 阶段 3.5 调用。

        Args:
            tool_funcs: @tool-decorated 函数列表（来自 create_sdk_tools）
            mcp_json_path: 外部 .mcp.json 路径（如 crawl-mcp 配置）
        """
        self._mcp_json_path = mcp_json_path
        self._saved_tool_funcs = tool_funcs  # 保存供 reconnect 使用

        options = self._build_options(
            tool_funcs=tool_funcs,
            max_turns=config.max_tool_rounds,
        )

        self.client = ClaudeSDKClient(options=options)
        t0 = time.monotonic()
        await self.client.connect()
        elapsed = time.monotonic() - t0
        self._connected = True
        log.info(
            "SDK Client 已连接 (%.2fs) | 模型: %s",
            elapsed,
            config.anthropic_model,
        )

    async def stop(self):
        """断开 SDK 客户端。在 Agent.stop() 中调用。"""
        if self.client and self._connected:
            try:
                await self.client.disconnect()
                log.info("SDK Client 已断开")
            except Exception as e:
                log.warning("SDK Client 断开异常 (忽略): %s", e)
            finally:
                self._connected = False

    async def reconnect(self):
        """断线后重建连接。用保存的 tool_funcs / mcp_json_path 完整重建。"""
        log.warning("SDK Client 尝试重连...")
        try:
            await self.stop()
        except Exception:
            pass
        if self._saved_tool_funcs is not None:
            await self.start(
                tool_funcs=self._saved_tool_funcs,
                mcp_json_path=self._mcp_json_path,
            )
            log.info("SDK Client 重连完成")
        else:
            log.error("SDK Client 无法重连: 无保存的 tool_funcs")

    # ---- Options 构建 ----

    def _build_options(
        self,
        tool_funcs: list | None = None,
        max_turns: int = 10,
    ) -> ClaudeAgentOptions:
        """构建 ClaudeAgentOptions。"""
        # 环境变量：API Key + 兼容 Base URL
        env: dict[str, str] = {"ANTHROPIC_API_KEY": config.anthropic_api_key}
        if config.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = config.anthropic_base_url

        # MCP servers: in-process "mmag" server + optional external .mcp.json
        mcp_servers: dict[str, Any] = {}

        if tool_funcs:
            mmag_server = create_sdk_mcp_server(
                name="mmag", version="0.1.0", tools=tool_funcs
            )
            mcp_servers["mmag"] = mmag_server

        if self._mcp_json_path:
            mcp_servers["external"] = str(self._mcp_json_path)

        options = ClaudeAgentOptions(
            model=config.anthropic_model,
            max_turns=max_turns,
            system_prompt=(
                "你是一个 mmag (Mattermost AI Agent) 助手。"
                "你可以使用工具查询消息、搜索知识库、分析链接等。"
                "回答简洁、准确、有帮助。"
            ),
            permission_mode="bypassPermissions",
            mcp_servers=mcp_servers if mcp_servers else None,
            allowed_tools=[],  # bypassPermissions 下允许所有工具（不能传 None，SDK 内部会 list(None) 报错）
            env=env,
            setting_sources=[],  # 不加载项目 CLAUDE.md
        )
        return options

    # ---- Prompt 构建 ----

    def _build_prompt(self, messages: list[dict], system: str = "") -> str:
        """将 Anthropic 格式消息列表展平为 prompt 字符串。

        处理多模态消息: text block 直出, image block 标注为 [图片附件]。
        System prompt 作为 [System] 前缀注入。
        """
        parts: list[str] = []
        if system:
            parts.append(f"[System]\n{system}")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                parts.append(f"[{role.capitalize()}]\n{content}")
            elif isinstance(content, list):
                # 多模态: content 是 [text_block, image_block, ...]
                text_parts: list[str] = []
                image_count = 0
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "image":
                            source = block.get("source", {})
                            media_type = source.get("media_type", "image")
                            text_parts.append(f"[图片附件: {media_type}]")
                            image_count += 1
                combined_text = "\n".join(filter(None, text_parts))
                if combined_text:
                    label = f"[{role.capitalize()}]"
                    if image_count > 0:
                        label += f" (含{image_count}张图片)"
                    parts.append(f"{label}\n{combined_text}")

        return "\n\n".join(parts)

    # ---- 核心查询执行 ----

    async def _execute_query(self, prompt: str) -> tuple[str, bool]:
        """执行 query() + receive_response() 循环。

        Returns:
            (response_text, had_error) 元组
        """
        if not self.client or not self._connected:
            raise SDKLLMError("SDK Client 未连接")

        self.call_count += 1
        t0 = time.monotonic()
        text_parts: list[str] = []
        tool_calls_count = 0
        is_error = False
        result_msg: ResultMessage | None = None

        try:
            await self.client.query(prompt)
            async for msg in self.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_calls_count += 1
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
                    if msg.is_error:
                        is_error = True
                        log.warning(
                            "SDK ResultMessage.is_error: %s",
                            getattr(msg, "errors", None),
                        )
                    # 记录成本
                    cost = getattr(msg, "total_cost_usd", None) or 0.0
                    if cost > 0:
                        log.info("SDK 本次调用成本: $%.4f", cost)
                    usage = getattr(msg, "usage", None)
                    if usage:
                        log.debug(
                            "SDK tokens: input=%d output=%d",
                            usage.get("input_tokens", 0),
                            usage.get("output_tokens", 0),
                        )
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("SDK query 失败 (%.3fs): %s", elapsed, e, exc_info=True)
            is_error = True
            # 检测断线
            err_str = str(e).lower()
            if "disconnect" in err_str or "connection" in err_str or "broken" in err_str:
                self._connected = False
                log.warning("检测到连接断开, 下次调用将尝试重连")
            raise SDKLLMError(str(e)) from e

        elapsed = time.monotonic() - t0
        raw_text = "\n".join(text_parts)

        log.debug(
            "SDK query 完成 (%.3fs) | turns=%d tools=%d chars=%d",
            elapsed,
            result_msg.num_turns if result_msg else 0,
            tool_calls_count,
            len(raw_text),
        )

        return raw_text, is_error

    # ---- 公共 API (与 LLM 类签名一致) ----

    async def agent_loop(
        self,
        messages: list[dict],
        system: str = "",
        tools: Any = None,          # API 兼容, 忽略 (SDK 内置工具)
        tool_registry: Any = None,   # API 兼容, 忽略
        max_rounds: int | None = None,
        max_tokens: int = 4096,      # API 兼容, 忽略
    ) -> str:
        """通过 SDK 执行 agentic 循环。公共 API 与 LLM.agent_loop 完全一致。

        SDK 内部处理多轮 tool-use 循环, 本 adapter 只负责:
        1. 展平 messages 为 prompt 字符串
        2. 调用 query + receive_response
        3. 提取文本 + artifact stripping
        4. 返回最终回复字符串
        """
        prompt = self._build_prompt(messages, system)

        try:
            # 自动重连
            if not self._connected:
                await self.reconnect()

            raw_text, is_error = await self._execute_query(prompt)

            # 过滤国产模型训练痕迹
            cleaned = _strip_model_artifacts(raw_text).strip()

            if not cleaned:
                if is_error:
                    raise SDKLLMError("SDK 返回错误且无文本")
                # 空 → 触发 Plan D 兜底
                return "⚠️ 处理超时，请重试"

            return cleaned

        except SDKLLMError:
            raise
        except Exception as e:
            log.error("agent_loop 异常: %s", e, exc_info=True)
            raise SDKLLMError(str(e)) from e

    async def chat(
        self, messages: list[dict], system: str = "", max_tokens: int = 1024
    ) -> str:
        """单轮对话 via SDK。用于 Plan D 兜底和 MemoryCompactor。"""
        prompt = self._build_prompt(messages, system)

        try:
            if not self._connected:
                await self.reconnect()

            raw_text, _is_error = await self._execute_query(prompt)
            cleaned = _strip_model_artifacts(raw_text).strip()
            return cleaned if cleaned else "(模型返回为空)"
        except Exception as e:
            raise SDKLLMError(str(e)) from e

    async def chat_with_system(
        self, system_prompt: str, user_message: str, max_tokens: int = 1024
    ) -> str:
        """带系统提示词的对话快捷方法（MemoryCompactor 兼容）。"""
        return await self.chat(
            messages=[{"role": "user", "content": user_message}],
            system=system_prompt,
            max_tokens=max_tokens,
        )
