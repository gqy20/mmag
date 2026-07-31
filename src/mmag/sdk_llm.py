"""
SDK LLM Adapter — 封装 Claude Agent SDK 持久客户端，提供与 LLM 类完全一致的公共 API。

Runtime Adapter 使用的 API:
  - agent_loop(messages, system, tools, tool_registry, max_rounds) -> str
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
    from collections.abc import AsyncIterator

log = get_logger(__name__)


# ============================================================
# 异常
# ============================================================


class SDKLLMError(Exception):
    """SDK LLM 调用失败的领域异常 — 对标 LLMError"""


# ============================================================
# 工具权限控制 — 三层防护: sandbox + 路径白名单 + 工具黑名单
# ============================================================

# 项目根目录 — 所有文件操作的允许范围边界
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

# 需要路径检查的只读工具（参数含 file_path / path / pattern）
_PATH_SENSITIVE_TOOLS = frozenset({"Read", "Grep", "Glob", "LSP"})

# 完全不需要路径检查的工具（网络/内部状态）
_PATH_SAFE_TOOLS = frozenset(
    {
        "WebFetch",  # URL 访问，不涉及本地文件
        "WebSearch",  # 网络搜索，不涉及本地文件
        "TodoWrite",  # SDK 内部状态，无文件系统操作
        "Task",  # SDK 内部状态，无文件系统操作
    }
)

# 所有允许的 CLI 内置工具
_CLI_SAFE_TOOLS = _PATH_SENSITIVE_TOOLS | _PATH_SAFE_TOOLS

# 禁止的危险工具（无论什么情况都不允许）
CLI_DANGEROUS_TOOLS = frozenset(
    {
        "Bash",  # 🔴 执行任意 shell 命令
        "Write",  # 🔴 写入/创建任意文件
        "Edit",  # 🔴 编辑文件 (sed-like)
    }
)

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


def _resolve_path(path: str | None) -> str | None:
    """解析路径并返回绝对路径。返回 None 表示无法解析或非法路径。"""
    if not path or not isinstance(path, str):
        return None
    try:
        p = Path(path).expanduser().resolve()
        return str(p)
    except (ValueError, OSError):
        return None


def _is_path_allowed(path: str) -> bool:
    """严格路径白名单检查：只允许项目根目录及其子目录内的文件。

    阻止:
      - 绝对路径跳出项目目录 (如 /etc/passwd, ~/.ssh/id_rsa)
      - symlink 跳出 (resolve() 会解引用)
      - .. 穿越攻击
    """
    resolved = _resolve_path(path)
    if not resolved:
        return False
    resolved_path = Path(resolved)
    project_root = Path(_PROJECT_ROOT).resolve()
    if not resolved_path.is_relative_to(project_root):
        log.warning("路径越界: %s (不在 %s 内)", resolved, _PROJECT_ROOT)
        return False
    return True


def _extract_paths_from_input(tool_name: str, input_data: dict[str, Any]) -> list[str]:
    """从工具输入参数中提取所有可能的文件路径。

    不同工具的路径参数名不同:
      Read:     file_path
      Grep:     pattern (可能含路径通配)
      Glob:     pattern (路径通配)
      LSP:      file_path / uri
    """
    paths: list[str] = []

    # 直接路径参数
    for key in ("file_path", "path", "uri", "directory"):
        val = input_data.get(key)
        if val and isinstance(val, str):
            paths.append(val)

    # pattern 参数 (Grep/Glob 可能是路径模式)
    pattern = input_data.get("pattern")
    # 如果 pattern 看起来像路径（含 / 或 . 或 ~），也检查
    if isinstance(pattern, str) and pattern and any(c in pattern for c in ("/", ".", "~")):
        paths.append(pattern)

    # include/exclude 文件列表
    for key in ("include", "exclude", "files"):
        val = input_data.get(key)
        if isinstance(val, list):
            paths.extend(v for v in val if isinstance(v, str))
        elif isinstance(val, str):
            paths.append(val)

    return paths


async def _tool_permission_callback(
    tool_name: str,
    input_data: dict[str, Any],
    context,  # ToolPermissionContext
    *,
    allowed_mcp_tools: frozenset[str] = _DEFAULT_MCP_ALLOWED_TOOLS,
) -> Any:
    """can_use_tool 回调 — 三层动态权限决策。

    层级 1 — MCP 工具: 仅显式 allowlist 中的 mmag 能力放行
    层级 2 — 危险工具黑名单: Bash/Write/Edit → ❌ 无条件拒绝
    层级 3 — 安全工具路径检查: Read/Grep/Glob/LSP → 仅允许项目目录内
    层级 4 — 未知工具 → ❌ 默认拒绝（安全优先）
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    # ── 层级 1: MCP 工具 ──
    if tool_name.startswith("mcp__"):
        if tool_name in allowed_mcp_tools:
            log.debug("权限放行 [MCP allowlist]: %s", tool_name)
            return PermissionResultAllow()
        log.warning("权限拒绝 [未知 MCP]: %s", tool_name)
        return PermissionResultDeny(
            message=f"MCP 工具 '{tool_name}' 未在 mmag 显式白名单中",
        )

    # ── 层级 2: 危险工具无条件拒绝 ──
    if tool_name in CLI_DANGEROUS_TOOLS:
        log.warning(
            "权限拒绝 [危险]: %s (input=%s)",
            tool_name,
            str(input_data)[:200],
        )
        return PermissionResultDeny(
            message=f"工具 '{tool_name}' 在 bot 模式下被禁用（安全策略）",
        )

    # ── 层级 3: 安全工具 + 路径白名单检查 ──
    if tool_name in _PATH_SAFE_TOOLS:
        # WebFetch/WebSearch/TodoWrite/Task 不涉及本地文件
        log.debug("权限放行 [安全-无路径]: %s", tool_name)
        return PermissionResultAllow()

    if tool_name in _PATH_SENSITIVE_TOOLS:
        # 提取所有可能的路径参数
        candidate_paths = _extract_paths_from_input(tool_name, input_data)

        if candidate_paths:
            # 有路径参数 → 逐个检查
            for p in candidate_paths:
                if not _is_path_allowed(p):
                    log.warning(
                        "权限拒绝 [路径越界]: %s | tool=%s | path=%s",
                        tool_name,
                        tool_name,
                        p,
                    )
                    return PermissionResultDeny(
                        message=(
                            f"禁止访问项目外文件: {p}\n"
                            f"仅允许读取 {_PROJECT_ROOT} 及其子目录下的文件"
                        ),
                    )
            log.debug(
                "权限放行 [安全-路径OK]: %s (%d 个路径已校验)", tool_name, len(candidate_paths)
            )
            return PermissionResultAllow()
        else:
            # 无路径参数（罕见但可能）→ 放行（SDK 内部会处理错误）
            log.debug("权限放行 [安全-无路径]: %s (无路径参数)", tool_name)
            return PermissionResultAllow()

    # ── 层级 4: 未知工具默认拒绝 ──
    log.warning("权限拒绝 [未知]: %s", tool_name)
    return PermissionResultDeny(
        message=f"未知工具 '{tool_name}' 未在白名单中",
    )


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
            tool_funcs: @tool-decorated 函数列表（来自 create_sdk_tools）
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
                "- Read / Grep / Glob: 阅读和搜索项目文件"
                "回答简洁、准确、有帮助。"
            ),
            permission_mode="default",
            mcp_servers=mcp_servers,
            # 不设 allowed_tools → 工具保持可见，执行时由 can_use_tool 做显式策略判断
            # 注意: 必须用 "default" 而非 "bypassPermissions":
            #   1. bypassPermissions 会被 SDK 转为 --dangerously-skip-permissions CLI flag,
            #      该 flag 在 root/sudo 下被 CLI 拒绝运行
            #   2. bypassPermissions 会 shadow can_use_tool 回调 (回调永不执行),
            #      使三层权限防护全部失效
            disallowed_tools=list(CLI_DANGEROUS_TOOLS),  # 安全网: 黑名单危险工具
            can_use_tool=partial(
                _tool_permission_callback,
                allowed_mcp_tools=visible_mcp_tools,
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

    # ---- 公共 API (与 LLM 类签名一致) ----

    async def agent_loop(
        self,
        messages: list[dict],
        system: str = "",
        tools: Any = None,  # API 兼容, 忽略 (SDK 内置工具)
        tool_registry: Any = None,  # API 兼容, 忽略
        max_rounds: int | None = None,
        max_tokens: int = 4096,  # API 兼容, 忽略
    ) -> str:
        """通过 SDK 执行 agentic 循环。公共 API 与 LLM.agent_loop 完全一致。

        SDK 内部处理多轮 tool-use 循环, 本 adapter 只负责:
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
            log.error("agent_loop 异常: %s", e, exc_info=True)
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
