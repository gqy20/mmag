"""
SDK LLM Adapter — 封装 Claude Agent SDK 持久客户端，提供与 LLM 类完全一致的公共 API。

Runtime Adapter 使用的 API:
  - run_agent(messages, system) -> str
  - chat(messages, system, max_tokens) -> str

生命周期:
  - start(tool_funcs) -> connect()
  - stop() -> disconnect()
  - reconnect() -> 断线后自动重建连接
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import ClaudeAgentOptions, McpServerConfig, TextBlock, ToolUseBlock

from .capabilities import CapabilityContext, get_capability_context
from .config import config
from .logger import get_logger
from .model_artifacts import strip_model_artifacts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

log = get_logger(__name__)


# ============================================================
# 异常
# ============================================================


class SDKLLMError(Exception):
    """SDK LLM 调用失败的领域异常 — 对标 LLMError"""


# SDK 内注册到 in-process "mmag" MCP server 的已知能力。
# 新增工具必须显式加入这里，否则权限回调默认拒绝执行。
_DEFAULT_MCP_ALLOWED_TOOLS = frozenset(
    f"mcp__mmag__{name}"
    for name in (
        "get_posts",
        "search_messages",
        "search_knowledge",
        "get_channel_info",
        "save_knowledge",
        "get_user_profile",
        "analyze_link",
        "send_file",
    )
)


async def _tool_permission_callback(
    tool_name: str,
    input_data: dict[str, Any],
    context,  # ToolPermissionContext
    *,
    allowed_mcp_tools: frozenset[str] = _DEFAULT_MCP_ALLOWED_TOOLS,
    context_provider: Callable[[], CapabilityContext | None] = get_capability_context,
) -> Any:
    """Allow only a bound MCP capability visible to the current Package run."""
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    del input_data, context
    if not tool_name.startswith("mcp__mmag__") or tool_name not in allowed_mcp_tools:
        log.warning("SDK 权限拒绝 [未绑定能力]: %s", tool_name)
        return PermissionResultDeny(
            message=f"能力 '{tool_name}' 未绑定到 mmag SDK Runtime",
        )
    active = context_provider()
    capability_name = tool_name.removeprefix("mcp__mmag__")
    if active is None or capability_name not in active.allowed_capabilities:
        log.warning("SDK 权限拒绝 [Package 不可见]: %s", capability_name)
        return PermissionResultDeny(
            message=f"能力 '{capability_name}' 不在当前 Agent Package allowlist",
        )
    log.debug("SDK 权限放行 [Package allowlist]: %s", capability_name)
    return PermissionResultAllow()


# ============================================================
# SDK LLM Adapter 类
# ============================================================


class SDKLLM:
    """Optional persistent Claude SDK client used through its Runtime adapter."""

    def __init__(self):
        self.client: ClaudeSDKClient | None = None
        self._connected = False
        self._saved_tool_funcs: list | None = None
        self.call_count = 0
        self.model = config.anthropic_model
        # ClaudeSDKClient owns a detached MCP reader task.  ContextVars from a
        # later query do not flow into that already-running task, so the SDK
        # transport uses one serialized, immutable request context bridge.
        self._query_lock = asyncio.Lock()
        self._active_capability_context: CapabilityContext | None = None

    def get_capability_context(self) -> CapabilityContext | None:
        """Expose the request currently serialized through the SDK transport."""
        return self._active_capability_context

    # ---- 生命周期 ----

    async def start(self, tool_funcs: list | None = None):
        """初始化并连接持久 SDK 客户端。在 Agent.start() 阶段 3.5 调用。

        Args:
            tool_funcs: @tool-decorated Capability binding 列表
        """
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
        """断线后用保存的 Capability bindings 重建连接。"""
        log.warning("SDK Client 尝试重连...")
        with suppress(Exception):
            await self.stop()
        if self._saved_tool_funcs is not None:
            await self.start(tool_funcs=self._saved_tool_funcs)
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

        # 所有内置和外部能力都已在上层进入 Catalog，这里只生成一个 SDK server。
        mcp_servers: dict[str, McpServerConfig] = {}
        all_tool_funcs: list = list(tool_funcs) if tool_funcs else []

        if all_tool_funcs:
            mmag_server = create_sdk_mcp_server(name="mmag", version="0.1.0", tools=all_tool_funcs)
            mcp_servers["mmag"] = mmag_server

        visible_mcp_tools = frozenset(f"mcp__mmag__{tool_def.name}" for tool_def in all_tool_funcs)

        options = ClaudeAgentOptions(
            model=config.anthropic_model,
            max_turns=max_turns,
            system_prompt=(
                "你是一个 mmag (Mattermost AI Agent) 助手。"
                "你可以使用以下工具:"
                "- get_posts / search_messages / search_knowledge: 查询 Mattermost 消息和知识库"
                "- analyze_link: 分析链接内容"
                "- allowlisted external MCP capabilities: 使用已授权的企业系统"
                "回答简洁、准确、有帮助。"
            ),
            permission_mode="default",
            mcp_servers=mcp_servers,
            allowed_tools=sorted(visible_mcp_tools),
            # 注意: 必须用 "default" 而非 "bypassPermissions":
            #   1. bypassPermissions 会被 SDK 转为 --dangerously-skip-permissions CLI flag,
            #      该 flag 在 root/sudo 下被 CLI 拒绝运行
            #   2. bypassPermissions 会 shadow can_use_tool 回调 (回调永不执行),
            #      使三层权限防护全部失效
            can_use_tool=partial(
                _tool_permission_callback,
                allowed_mcp_tools=visible_mcp_tools,
                context_provider=self.get_capability_context,
            ),
            env=env,
            setting_sources=[],  # 不加载项目 CLAUDE.md
            cwd=str(Path(__file__).resolve().parents[2]),  # 限制工作目录为项目根目录
            # ── Session 隔离 ──
            session_id=str(uuid.uuid4()),  # 独立 session ID（用于日志追踪）
            extra_args={
                "no-session-persistence": None
            },  # 不写 session 文件到 ~/.claude/projects/ (SDK 自动加 -- 前缀) (SDK 自动加 -- 前缀)
        )
        return options

    # ---- 内容构建 ----

    def _build_content_blocks(self, messages: list[dict], system: str = "") -> list[dict]:
        """将消息列表转换为结构化 content blocks, 保留 image blocks。

        替代旧的 _build_prompt() — 不再展平为文本字符串,
        image block 原样保留, 使 CLI 能传递给 Anthropic Vision API。

        text block 带 [role] 标签 (与旧实现一致), image block 无标签。
        """
        blocks: list[dict] = []
        if system:
            blocks.append({"type": "text", "text": f"[System]\n{system}"})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                blocks.append({"type": "text", "text": f"[{role.capitalize()}]\n{content}"})
            elif isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "image":
                        if text_parts:
                            blocks.append(
                                {
                                    "type": "text",
                                    "text": f"[{role.capitalize()}]\n"
                                    + "\n".join(filter(None, text_parts)),
                                }
                            )
                            text_parts = []
                        blocks.append(block)
                if text_parts:
                    blocks.append(
                        {
                            "type": "text",
                            "text": f"[{role.capitalize()}]\n"
                            + "\n".join(filter(None, text_parts)),
                        }
                    )

        return blocks

    async def _message_stream(self, content: list[dict]) -> AsyncIterator[dict[str, Any]]:
        """构造 stream-json 格式的单条用户消息 (AsyncIterable)。

        SDK client.query(AsyncIterable) 路径会逐条 json.dumps 写入 CLI stdin,
        绕过 query(str) 路径的 content-type=str 限制, 使 image blocks 原样到达 CLI。
        """
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    # ---- 核心查询执行 ----

    async def _execute_query(
        self,
        content: list[dict],
        *,
        capability_context: CapabilityContext | None = None,
    ) -> tuple[str, bool]:
        """Serialize the persistent SDK stream and scope its capability context."""
        async with self._query_lock:
            self._active_capability_context = capability_context
            try:
                return await self._execute_query_unlocked(content)
            finally:
                self._active_capability_context = None

    async def _execute_query_unlocked(self, content: list[dict]) -> tuple[str, bool]:
        """执行 query() + receive_response() 循环。

        Args:
            content: 结构化 content blocks (含 text/image blocks)

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

        # Session 追踪（用于日志关联，不持久化到磁盘）
        _session_tag = getattr(self.client, "_session_id", f"call-{self.call_count}")

        _image_count = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
        log.debug(
            "SDK query #%d | session=%s | blocks=%d images=%d",
            self.call_count,
            _session_tag,
            len(content),
            _image_count,
        )

        try:
            await self.client.query(self._message_stream(content))
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
            "SDK query 完成 (%.3fs) | session=%s | turns=%d tools=%d chars=%d",
            elapsed,
            getattr(result_msg, "session_id", _session_tag) if result_msg else _session_tag,
            result_msg.num_turns if result_msg else 0,
            tool_calls_count,
            len(raw_text),
        )

        return raw_text, is_error

    async def run_agent(
        self,
        messages: list[dict],
        system: str = "",
    ) -> str:
        """通过 SDK 执行 agentic 循环。

        SDK 内部处理多轮 tool-use 循环，本方法只负责：
        1. 构建 content blocks (保留 image blocks)
        2. 调用 query + receive_response
        3. 提取文本 + artifact stripping
        4. 返回最终回复字符串
        """
        content = self._build_content_blocks(messages, system)

        try:
            # 自动重连
            if not self._connected:
                await self.reconnect()

            raw_text, is_error = await self._execute_query(
                content,
                capability_context=get_capability_context(),
            )

            # 过滤国产模型训练痕迹
            cleaned = strip_model_artifacts(raw_text).strip()

            if not cleaned:
                if is_error:
                    raise SDKLLMError("SDK 返回错误且无文本")
                # 空 → 触发 Plan D 兜底
                return "⚠️ 处理超时，请重试"

            return cleaned

        except SDKLLMError:
            raise
        except Exception as e:
            log.error("SDK Agent 执行异常: %s", e, exc_info=True)
            raise SDKLLMError(str(e)) from e

    async def chat(self, messages: list[dict], system: str = "", max_tokens: int = 1024) -> str:
        """单轮对话 via SDK。用于 Plan D 兜底和 MemoryCompactor。"""
        content = self._build_content_blocks(messages, system)

        try:
            if not self._connected:
                await self.reconnect()

            raw_text, _is_error = await self._execute_query(content)
            cleaned = strip_model_artifacts(raw_text).strip()
            return cleaned if cleaned else "(模型返回为空)"
        except Exception as e:
            raise SDKLLMError(str(e)) from e
