"""
核心 Agent — 消息处理 + 响应编排 (支持 Agentic Tool Use + 多模态)

WebSocket 连接管理已拆分到 ws_client.py, 记忆压缩拆分到 memory_compactor.py
"""

import asyncio
import base64
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .capabilities import CapabilityContext, bind_capability_context
from .client import PROP_FROM_BOT, PROP_TRUE, MMClient
from .config import _log_config_loading, _secret_status, config
from .llm import LLM
from .logger import get_logger, trace
from .mcp_bridge import MCPClientBridge
from .memory import Memory
from .memory_compactor import MemoryCompactor
from .prompts import prompts
from .runtimes import (
    AgentRuntime,
    AgentRuntimeError,
    ClaudeSDKRuntimeAdapter,
    LegacyRuntimeAdapter,
    RunContext,
    RunRequest,
)
from .sdk_llm import SDKLLM
from .tools import ToolRegistry, build_builtin_tools
from .ws_client import WebSocketClient

log = get_logger(__name__)


# MIME 精确值 → 视为文本文档 (text/* 前缀单独判断)
_TEXT_MIME_EXACT = frozenset({
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/x-yml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-toml",
    "application/toml",
    "application/x-latex",
    "application/x-httpd-php",
    "application/sql",
    "application/graphql",
})
# 文件扩展名 → 视为文本文档 (当 MIME 不可靠时, 如 application/octet-stream)
_TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".log",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".json5", ".jsonl",
    ".xml", ".html", ".htm", ".css", ".csv", ".tsv",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs",
    ".sh", ".bash", ".zsh", ".fish",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".java", ".kt", ".scala", ".rb", ".php", ".pl",
    ".sql", ".graphql", ".gql",
    ".lua", ".r", ".dart", ".swift", ".clj",
    ".vue", ".svelte",
    ".env", ".gitignore", ".dockerfile",
    ".tex", ".bib",
})


def _is_text_attachment(mime: str, filename: str) -> bool:
    """判断附件是否为文本文档 (可下载并 UTF-8 解码)

    先查 MIME 前缀/精确匹配, 再用文件扩展名兜底
    (Mattermost 对 yaml 等 MIME 可能返回 application/octet-stream)
    """
    if mime.startswith("text/"):
        return True
    if mime in _TEXT_MIME_EXACT:
        return True
    # octet-stream 或空 MIME → 查扩展名兜底
    if not mime or mime == "application/octet-stream":
        ext = Path(filename).suffix.lower()
        if ext in _TEXT_EXTENSIONS:
            return True
    return False


class Agent:
    """Mattermost AI Agent 主类 — 支持 Agentic Tool Use"""

    def __init__(self):
        self.config = config
        self.mm = MMClient()
        self.llm = LLM()
        self.sdk_llm = SDKLLM()  # SDK adapter (与 LLM 共存, 通过 config.use_sdk_llm 切换)
        self.memory = Memory(config.memory_db_path)

        # 工具注册系统
        self.tool_registry = ToolRegistry()
        builtin_tools = build_builtin_tools(self.mm, self.memory)
        for t in builtin_tools:
            self.tool_registry.register(t)
        log.info(f"工具系统就绪: {len(builtin_tools)} 个内置工具")

        # 应用层只依赖 AgentRuntime；启动阶段可在 SDK/Legacy Adapter 间切换。
        self.runtime: AgentRuntime = LegacyRuntimeAdapter(
            self.llm,
            tool_registry=self.tool_registry,
        )

        # MCP 外部工具桥接（读取 .mcp.json，连接外部 Server）
        self.mcp_bridge = MCPClientBridge(
            self.tool_registry,
            allowed_tools=config.mcp_allowed_tools,
        )

        # 运行状态
        self.start_time = time.time()
        self.stats = {"messages": 0, "responses": 0, "dropped_messages": 0}
        self.running = False

        # 频道级状态
        self.working_memory: dict[str, list] = {}  # 频道 → 最近消息窗口

        # Bot 身份 (启动时获取)
        self.bot_user_id = ""
        self.bot_username = ""

        # 记忆压缩器 (长期记忆层管理)
        self.compactor = MemoryCompactor(
            memory=self.memory,
            runtime=self.runtime,
            mm_client=self.mm,
            config=self.config,
        )

        # WebSocket 客户端 (启动时构造, 调用 ws.run() 进入事件循环)
        self.ws: WebSocketClient | None = None

    async def start(self):
        """启动 Agent — 按 Mattermost 官方 WebSocket 协议实现"""

        # 阶段 0: 打印配置快照
        _log_config_loading()

        log.info("=" * 50)
        log.info("  🤖 Agent 启动中...")
        log.info("=" * 50)

        # 阶段 1: 获取 Bot 身份（基于 MM_TOKEN 调 /users.me,user_id 与 username 一起返回）
        log.info("[1/5] 获取 Bot 身份...")
        me = self.mm.get_me()
        self.bot_user_id = me["id"]
        self.bot_username = me["username"]
        log.info(f"       ✅ Bot: @{self.bot_username} ({self.bot_user_id}) [来源: API]")

        # 阶段 2: LLM 配置检查
        log.info("[2/5] 检查 LLM 配置...")
        log.info(f"       模型: {config.anthropic_model}")
        log.info(f"       API:  {config.anthropic_base_url or '默认 (api.anthropic.com)'}")
        log.info("       Key:  %s", _secret_status(config.anthropic_api_key))
        if not config.anthropic_api_key:
            log.error("       ❌ ANTHROPIC_API_KEY 未设置! 请检查 .env")
            self.running = False
            return

        # 阶段 3: 连接 MCP 外部工具 Server
        log.info("[3/5] 加载 MCP 外部工具...")
        try:
            mcp_count = await self.mcp_bridge.load_and_connect()
            if mcp_count > 0:
                total_mcp_tools = sum(
                    1 for t in self.tool_registry.get_all() if t.name.startswith("mcp_")
                )
                log.info(
                    f"       ✅ MCP 已连接 {mcp_count} 个 Server, 注册 {total_mcp_tools} 个外部工具"
                )
            else:
                log.info("       ⏭️ 无 MCP 配置 (.mcp.json 不存在或为空)")
        except Exception as e:
            log.warning("       ⚠️ MCP 加载失败（不影响运行）: %s", e)

        # 阶段 3.5: 初始化 SDK LLM Client (持久连接)
        log.info("[3.5/5] 初始化 SDK LLM Client...")
        if config.use_sdk_llm:
            try:
                from .sdk_tools import create_sdk_tools

                sdk_tool_funcs = create_sdk_tools(
                    self.mm,
                    self.memory,
                    context_provider=self.sdk_llm.get_capability_context,
                )
                sdk_tool_funcs.extend(self.mcp_bridge.get_sdk_bindings())

                await self.sdk_llm.start(
                    tool_funcs=sdk_tool_funcs,
                )
                self.runtime = ClaudeSDKRuntimeAdapter(
                    self.sdk_llm,
                    tool_registry=self.tool_registry,
                )
                self.compactor.runtime = self.runtime
                log.info("       ✅ SDK LLM 就绪 (持久连接)")
            except Exception as e:
                log.error("       ❌ SDK LLM 初始化失败, 回退到 legacy LLM: %s", e)
                config.use_sdk_llm = False  # 自动降级
        else:
            log.info("       ⏭️ SDK LLM 未启用 (use_sdk_llm=False), 使用 legacy LLM")

        # 阶段 4: 预加载频道消息到缓存
        log.info("[4/5] 预加载频道消息...")
        try:
            channels_to_load = []

            # 优先: 如果指定了具体频道 ID
            if config.mm_channel_id:
                ch_info = self.mm.get_channel(config.mm_channel_id)
                if ch_info and "name" in ch_info:
                    channels_to_load.append(ch_info)
                    log.info(
                        f"       📍 指定频道: {ch_info.get('display_name', config.mm_channel_id[:8])}"
                    )

            # 其次: 按 Team 加载所有频道
            elif config.mm_team_id:
                channels = self.mm._get(f"/teams/{config.mm_team_id}/channels")
                channels_to_load = list(channels)[:10]
                log.info(f"       📍 Team 下 {len(channels)} 个频道")

            # 都没指定: 不预加载
            else:
                log.info("       ⏭️ 未指定 Team/Channel ID，跳过预加载 (将实时获取)")

            total_msgs = 0
            total_new = 0
            for ch in channels_to_load:
                ch_id = ch["id"]
                # 1) Backfill: 拉本地最新时间戳之前的历史,补全到 message_log
                new_count = self._backfill_channel(ch_id)
                total_new += new_count
                # 2) 从本地 message_log 读最近 N 条作为初始上下文
                #    (不重复调 log_message — backfill 已经写过了,本就是全的;
                #     从本地读能保证 backfill 写入的字段 [username/create_at 等] 一致)
                posts = self.memory.get_recent_messages(
                    ch_id, limit=config.max_context_messages
                )
                self.working_memory[ch_id] = posts
                total_msgs += len(posts)
                log.info(
                    f"       📂 {ch.get('display_name', ch_id[:8]):<15} {len(posts):>3} 条 (新增 {new_count})"
                )
            if channels_to_load:
                log.info(
                    f"       ✅ 共 {len(channels_to_load)} 个频道, {total_msgs} 条消息已加载, backfill 新增 {total_new} 条"
                )
        except Exception as e:
            log.warning(f"       ⚠️ 预加载频道失败: {e}")
            log.warning("       (不影响运行，将使用空上下文启动)")

        # 阶段 5: 启动 WebSocket 客户端 (封装了连接/握手/心跳/重连)
        log.info("[5/5] 建立 WebSocket 连接 (官方协议)...")
        self.running = True  # 必须在进入循环前设置!

        self.ws = WebSocketClient(
            url=config.ws_url,
            token=config.mm_token,
            on_event=self._on_ws_event,
            on_response=self._on_ws_response,
        )

        # 阶段 5 收尾：提示就绪
        log.info("🎯 进入事件监听循环...")
        log.info("       触发策略: @/DM/thread 走硬规则，其他 LLM 自主决策")
        log.info(f"       上下文窗口: {config.max_context_messages} 条")
        log.info("       纯自然语言驱动，无命令")
        log.info("")
        log.info("────── Agent 就绪，等待消息 ──────")

        # 阻塞运行 (内部自动重连), 直到 stop() 调 ws.close()
        await self.ws.run()

    async def _on_ws_event(self, msg: dict):
        """WebSocket 服务端事件回调 (由 ws_client 在 JSON 解析 + 序列号校验后调用)

        协议区分两种消息 (见 websocket_client.go Listen()):
          1. **Event** (服务端推送): 有 `event` 字段, 带递增 `seq` — 走到这里
          2. **Response** (请求回复): 有 `seq_reply` 字段 — 走 _on_ws_response

        ws_client 已经做了 JSON 解析 + 序列号校验/告警, 这里只做事件类型分发。
        """
        etype = msg.get("event", "")

        async def _noop(_m):
            pass

        handler_map = {
            "posted": self._on_posted,
            "post_edited": _noop,  # TODO
            "post_deleted": _noop,  # TODO
            "typing": _noop,
            "reaction_added": _noop,
            "reaction_removed": _noop,
            "status_change": _noop,
            "channel_created": _noop,
            "channel_updated": _noop,
            "user_updated": _noop,
            "ephemeral_message": _noop,
        }
        handler = handler_map.get(etype)
        if handler:
            await handler(msg)
        elif etype not in ("hello",):
            log.debug(f"       [未处理事件] {etype}")

    def _backfill_channel(self, channel_id: str) -> int:
        """从 Mattermost REST 拉取该频道历史,补全到 message_log。

        增量: 用本地最新 create_at 作为 since,只拉本地没有的。
        限流: 每页之间 sleep 0.1s,避免打爆 MM。
        幂等: INSERT OR IGNORE,已存在跳过。

        Returns: 本次新增写入的消息数。
        """
        import time

        latest_sec = self.memory.get_latest_message_ts(channel_id)
        # 本地最新时间戳(毫秒),首次启动为 0 = 拉全部
        latest_ms = int(latest_sec * 1000)

        new_count = 0
        page = 0
        per_page = 200
        while True:
            posts = self.mm.get_posts_page(channel_id, page=page, per_page=per_page)
            if not posts:
                break

            stop = False
            for p in posts:
                p_create_at = p.get("create_at", 0) or 0
                if latest_ms and p_create_at <= latest_ms:
                    # 拉到本地已有时间,后面更旧不用再拉
                    stop = True
                    break
                p["channel_id"] = channel_id
                p["username"] = self.mm.get_username(p.get("user_id", ""))
                if self.memory.log_message(p):
                    new_count += 1
                else:
                    # 失败可能是 (已存在 / 字段缺失 / DB 写失败) — 全部计入 dropped
                    self.stats["dropped_messages"] += 1

            if stop or len(posts) < per_page:
                break
            page += 1
            time.sleep(0.1)  # 限流

        return new_count

    def _on_ws_response(self, msg: dict):
        """WebSocket 客户端请求响应回调 (认证结果 / ping 回复等)"""
        seq_reply = msg.get("seq_reply", 0)
        status = msg.get("status", "?")
        err = msg.get("error")

        if err:
            log.error(f"       ❌ 响应错误 (seq={seq_reply}): {err}")
        elif status == "OK":
            log.debug(f"       ✅ 响应 OK (seq={seq_reply})")
        else:
            log.debug(f"       响应 (seq={seq_reply}): status={status}")

    async def _on_posted(self, event: dict):
        """处理 posted 事件 — 核心消息处理入口

        data.post 格式 (Mattermost 9.x+):
          1. 完整 dict 对象 (Python json.loads 后)
          2. JSON 字符串 (包含完整 post 对象，需二次解析)
        """
        data = event.get("data", {})
        raw_post = data.get("post")

        if not raw_post:
            return

        # 统一为 dict
        if isinstance(raw_post, dict):
            post = raw_post
        elif isinstance(raw_post, str):
            # 尝试解析为 JSON 字符串形式的完整 post 对象
            try:
                parsed = json.loads(raw_post)
            except json.JSONDecodeError as e:
                log.warning(f"       无法解析 post JSON ({str(raw_post)[:40]}...): {e}")
                return
            if not isinstance(parsed, dict) or "id" not in parsed or "message" not in parsed:
                log.warning(f"       解析后的 post 缺少 id/message 字段: {str(raw_post)[:40]}...")
                return
            post = parsed
        else:
            return

        # 跳过自己的消息
        user_id = post.get("user_id", "")
        if user_id == self.bot_user_id:
            return

        # 跳过系统消息 (有 type 字段的都是系统消息)
        post_type = post.get("type", "")
        if post_type:
            return

        # ── 频道/Team 过滤 ──
        channel_id = post.get("channel_id", "")

        # 如果配置了 MM_CHANNEL_ID，只处理该频道
        if config.mm_channel_id and channel_id != config.mm_channel_id:
            return
        if config.mm_channel_id:
            log.debug(f"       ✅ 频道匹配: {channel_id[:12]}...")

        # 如果配置了 MM_TEAM_ID，检查频道是否属于该 Team
        if config.mm_team_id:
            ch_info = self.mm.get_channel(channel_id)
            ch_team_id = ch_info.get("team_id", "")
            if ch_team_id != config.mm_team_id:
                return
            log.debug(f"       ✅ Team 匹配: {ch_team_id}")

        message = (post.get("message") or "").strip()
        # 放宽: 没文字但有附件也接受 (用户可能只发了张图,无配文)
        file_metas = (post.get("metadata") or {}).get("files") or []
        if not message and not file_metas:
            return

        # Mattermost 在重连后可能重放 posted 事件。持久化记录是当前阶段的
        # 幂等边界，避免重复下载附件、调用 Runtime 和发送回复。
        post_id = post.get("id", "")
        if post_id and self.memory.has_message(post_id):
            log.info("       ⏭️ 跳过重复消息: %s", post_id[:12])
            return

        self.stats["messages"] += 1

        # 补充 username
        post["username"] = self.mm.get_username(user_id)

        # 多模态: 下载图片/文本文档附件, 构造 content block 列表, 注入到 _build_context
        # 注意: _llm_content_blocks 是临时字段, 仅供本次 LLM 调用, 不入 message_log
        post["_llm_content_blocks"] = await self._build_attachment_blocks(
            file_metas,
            max_count=config.max_images_per_msg,
            max_bytes=config.max_image_bytes,
            max_text_chars=config.max_text_attachment_chars,
        )

        # 缓存消息
        if not self.memory.log_message(post):
            self.stats["dropped_messages"] += 1

        # Layer 1→2 双阈值管理（定期摘要 + 容量清理）— 委托给 MemoryCompactor
        # 每条消息都检查，compactor 内部有 guard 避免无效操作
        await self.compactor.maybe_compact(channel_id)

        # 更新工作内存（窗口与 LLM 上下文消费对齐，不乘 2）
        if channel_id not in self.working_memory:
            self.working_memory[channel_id] = []
        self.working_memory[channel_id].append(post)
        if len(self.working_memory[channel_id]) > config.max_context_messages:
            self.working_memory[channel_id] = self.working_memory[channel_id][
                -config.max_context_messages :
            ]

        # 更新用户画像（从消息行为自动推断话题/时段/风格）
        self.memory.update_profile_from_message(user_id, post["username"], post)

        # 开启交互追踪
        trace.new()
        trace.set_context(
            channel=channel_id[:12],
            user=post["username"],
            msg_type="mention",  # 默认，下面会根据触发类型更新
        )

        log.info("%s [%s] %s", trace.prefix(), post["username"], message[:80])

        # ====== 触发判定: 显式召唤(硬规则,不走 LLM 决策)======
        if self._is_explicit_invocation(post):
            trace.set_context(msg_type="mention")
            log.info("%s → 触发: 显式召唤 (@/DM/thread)", trace.prefix())
            await self._send_get_ack(post)
            typing_task = asyncio.create_task(
                self._typing_loop(post["channel_id"])
            )
            try:
                await self._respond(post, tag="mention")
            finally:
                typing_task.cancel()
            trace.clear()
            return

        # ====== 触发判定: LLM 自主决策(一次 LLM 调用)======
        trace.set_context(msg_type="decide")
        log.info("%s → LLM 决策: 是否要回应...", trace.prefix())
        response_text = await self._llm_decide_and_respond(post)
        trace.clear()

        if self._is_silent(response_text):
            log.info("🤐 %s → LLM 决定沉默: %s", trace.prefix(), message[:60])
            return

        log.info("💬 %s → LLM 决定回应: %s", trace.prefix(), message[:60])
        await self._send_message(post, response_text)

    def _is_explicit_invocation(self, post: dict) -> bool:
        """显式召唤: 硬规则,优先级最高,不走 LLM 决策

        包括:
          - @ 提及我
          - DM 私聊
          - thread 回复我的消息
        """
        message = post["message"].lower()

        # @ 提及
        if f"@{self.bot_username}" in message:
            return True

        # DM 私聊
        ch_info = self.mm.get_channel(post["channel_id"])
        if ch_info.get("type") == "D":
            return True

        # thread 回复我的消息
        root_id = post.get("root_id", "")
        return bool(root_id and self.memory.get_post_user(root_id) == self.bot_user_id)

    async def _llm_decide_and_respond(self, post: dict) -> str:
        """让 LLM 自主决定是否回应: 一次 LLM 调用,LLM 输出 <SILENT> 或回复文本

        设计:
          - 一次 LLM 调用完成"判断 + 生成" (无额外判定调用)
          - LLM 拿到的 system_prompt 里有"收手原则"指令
          - LLM 通过输出 <SILENT> 标记表达沉默
        """
        await self.typing_indicator(post["channel_id"])
        context = self._build_context(post, mention=False)
        try:
            result = await self._run_request(
                post,
                self._build_run_request(
                    post,
                    context,
                    capabilities=(),
                    max_rounds=config.max_tool_rounds,
                )
            )
            return result.text or ""
        except AgentRuntimeError as e:
            log.error("       ❌ LLM 决策异常: %s", e)
            return "<SILENT>"

    def _build_run_request(
        self,
        post: dict,
        prompt_context: dict,
        *,
        capabilities: tuple[dict, ...],
        max_rounds: int,
    ) -> RunRequest:
        """Translate Mattermost context into the provider-neutral Runtime input."""
        channel_id = post["channel_id"]
        channel = self.mm.get_channel(channel_id)
        team_id = channel.get("team_id") or "-"
        return RunRequest(
            context=RunContext(
                trace_id=trace.current,
                actor_id=post.get("user_id", ""),
                conversation_id=channel_id,
                scope=f"mattermost:{team_id}/{channel_id}",
            ),
            messages=tuple(prompt_context["messages"]),
            system_prompt=prompt_context["system"],
            capabilities=capabilities,
            max_rounds=max_rounds,
        )

    def _is_silent(self, text: str) -> bool:
        """解析 LLM 输出: 第一行是 <SILENT> 标记则视为沉默"""
        if not text:
            return True
        first_line = text.strip().split("\n", 1)[0].strip()
        return first_line == "<SILENT>" or first_line.startswith("<SILENT>")

    async def _send_message(self, post: dict, message: str) -> str | None:
        """实际发送消息到频道 (主聊天流)"""
        return await self.reply(post, message)

    async def _respond(self, post: dict, *, tag: str, max_rounds: int | None = None):
        """响应用户消息（支持 Agentic Tool Use）

        Args:
            post: 触发的消息
            tag: 日志标签（mention / chat）— 区分 @提及 vs 旁听
            max_rounds: Agentic 工具调用最大轮次（@提及 5，旁听 3）
        """
        t0 = time.monotonic()
        log.info("%s [%s] 构建上下文...", trace.prefix(), tag)
        await self.typing_indicator(post["channel_id"])
        context = self._build_context(post, mention=(tag == "mention"))
        # 默认走 config,调用方可临时覆盖(目前没用到,留扩展位)
        rounds = max_rounds if max_rounds is not None else config.max_tool_rounds
        log.info(
            "%s [%s] 调用 Agent Loop (上下文 %d 条消息)...",
            trace.prefix(),
            tag,
            len(context["messages"]),
        )
        try:
            runtime_result = await self._run_request(
                post,
                self._build_run_request(
                    post,
                    context,
                    capabilities=tuple(self.tool_registry.get_schema_list()),
                    max_rounds=rounds,
                )
            )
            response = runtime_result.text
        except AgentRuntimeError as e:
            # LLM 调用失败 (网络/SDK异常) — 给用户友好提示 + log 留底
            log.error("%s [%s] LLM 调用失败: %s", trace.prefix(), tag, e, exc_info=True)
            response = "⚠️ LLM 服务暂时不可用，请稍后再试。"

        elapsed = time.monotonic() - t0
        log.info(
            "%s [%s] Agent Loop 返回 (%.1fs, %d 字符): %s",
            trace.prefix(),
            tag,
            elapsed,
            len(response),
            response[:150],
        )
        if not response:
            log.warning("%s [%s] LLM 返回为空", trace.prefix(), tag)
        else:
            result = await self.reply(post, response)
            log.info("%s [%s] 回复已发送 post_id=%s", trace.prefix(), tag, result)

    async def _run_request(self, post: dict, request: RunRequest):
        """Execute one Runtime request with immutable, task-local capability context."""
        context = CapabilityContext(
            trace_id=request.context.trace_id,
            actor_id=request.context.actor_id,
            conversation_id=request.context.conversation_id,
            message_id=post.get("id", ""),
            message=post.get("message", ""),
            scope=request.context.scope,
        )
        with bind_capability_context(context):
            return await self.runtime.run(request)

    async def _build_attachment_blocks(
        self, file_metas: list[dict], *, max_count: int, max_bytes: int,
        max_text_chars: int = 50000,
    ) -> list[dict] | None:
        """从 Mattermost 附件元信息列表里下载图片/文本文档, 构造 content blocks

        支持的附件类型:
        - image/* → base64 image block (并发下载)
        - text/* + application/json → text block (UTF-8 解码, 截断保护)
        - 其他 (PDF/zip/...) → 占位文本

        Args:
            file_metas: post["metadata"]["files"] 中的附件元信息列表
            max_count: 最多下载几张图, 超过的用占位文本
            max_bytes: 单张图片字节上限, 超过的用占位文本
            max_text_chars: 文本附件字符上限, 超过截断

        Returns:
            成功时: list[dict] (content blocks); 无附件/全失败时: None
        """
        if not file_metas:
            return None

        # 1) 分类: 图片 / 文本文档 / 其他
        image_metas: list[dict] = []
        text_metas: list[dict] = []
        skipped_notes: list[str] = []
        for fmeta in file_metas:
            mime = (fmeta.get("mime_type") or "").lower()
            name = fmeta.get("name", "")
            if mime.startswith("image/"):
                image_metas.append(fmeta)
            elif _is_text_attachment(mime, name):
                text_metas.append(fmeta)
            else:
                skipped_notes.append(f"[附件: {fmeta.get('name', '?')} ({mime})]")

        # 2) 图片数量截断
        if len(image_metas) > max_count:
            for fmeta in image_metas[max_count:]:
                skipped_notes.append(f"[图片过多已跳过: {fmeta.get('name', '?')}]")
            image_metas = image_metas[:max_count]

        # 3) 预估大小过滤 (图片)
        image_download_list: list[tuple[str, str, str, int]] = []
        for fmeta in image_metas:
            fid = fmeta.get("id", "")
            name = fmeta.get("name", "?")
            mime = (fmeta.get("mime_type") or "").lower()
            size = fmeta.get("size") or 0
            if size and size > max_bytes:
                skipped_notes.append(f"[图片过大已跳过: {name} ({size} bytes)]")
                continue
            image_download_list.append((fid, name, mime, size))

        # 4) 文本文档预估大小过滤 (复用 max_bytes 避免下载超大文件)
        text_download_list: list[tuple[str, str, str]] = []  # (fid, name, mime)
        for fmeta in text_metas:
            fid = fmeta.get("id", "")
            name = fmeta.get("name", "?")
            mime = (fmeta.get("mime_type") or "").lower()
            size = fmeta.get("size") or 0
            if size and size > max_bytes:
                skipped_notes.append(f"[文本附件过大已跳过: {name} ({size} bytes)]")
                continue
            text_download_list.append((fid, name, mime))

        all_downloads = image_download_list + text_download_list
        if not all_downloads and not skipped_notes:
            return None

        # 5) 并发下载 (图片 + 文本文档一起 gather)
        if all_downloads:
            results = await asyncio.gather(
                *(self.mm.get_file_bytes_async(fid) for fid, *_ in all_downloads),
                return_exceptions=True,
            )
        else:
            results = []

        # 6) 拼装 content blocks
        content_blocks: list[dict] = []
        n_images = len(image_download_list)

        # 6a) 图片 → image blocks
        for i, (fid, name, mime, _declared_size) in enumerate(image_download_list):
            result = results[i]
            if isinstance(result, BaseException):
                log.warning("下载图片异常 file_id=%s: %s", fid[:12], result)
                skipped_notes.append(f"[图片下载失败: {name}]")
                continue
            if not result:
                skipped_notes.append(f"[图片下载失败: {name}]")
                continue
            data, actual_mime = result
            if len(data) > max_bytes:
                skipped_notes.append(f"[图片过大已跳过: {name} ({len(data)} bytes)]")
                continue

            try:
                b64 = base64.standard_b64encode(data).decode("ascii")
            except Exception as e:
                log.warning("base64 编码失败 file_id=%s: %s", fid[:12], e)
                skipped_notes.append(f"[图片编码失败: {name}]")
                continue

            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": actual_mime or mime,
                        "data": b64,
                    },
                }
            )
            log.info(
                "%s 已加载图片附件: %s (%d bytes, mime=%s)",
                trace.prefix(),
                name,
                len(data),
                actual_mime or mime,
            )

        # 6b) 文本文档 → text blocks
        for j, (fid, name, mime) in enumerate(text_download_list):
            result = results[n_images + j]
            if isinstance(result, BaseException):
                log.warning("下载文本附件异常 file_id=%s: %s", fid[:12], result)
                skipped_notes.append(f"[文本附件下载失败: {name}]")
                continue
            if not result:
                skipped_notes.append(f"[文本附件下载失败: {name}]")
                continue
            data, _actual_mime = result
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception as e:
                log.warning("文本附件解码失败 file_id=%s: %s", fid[:12], e)
                skipped_notes.append(f"[文本附件解码失败: {name}]")
                continue

            truncated = len(text) > max_text_chars
            if truncated:
                text = text[:max_text_chars] + f"\n\n[... 已截断, 原文 {len(text)} 字符 ...]"

            content_blocks.append(
                {
                    "type": "text",
                    "text": f"[附件: {name}]\n{text}",
                }
            )
            log.info(
                "%s 已加载文本附件: %s (%d bytes%s, mime=%s)",
                trace.prefix(),
                name,
                len(data),
                ", 已截断" if truncated else "",
                mime,
            )

        if not content_blocks and not skipped_notes:
            return None
        if not content_blocks:
            log.info(
                "%s 所有附件均无法加载, 降级为文本: %s",
                trace.prefix(),
                "; ".join(skipped_notes),
            )
            return None

        return content_blocks + (
            [{"type": "text", "text": "附件说明: " + "; ".join(skipped_notes)}]
            if skipped_notes
            else []
        )

    def _format_user_profile_summary(self, user_id: str) -> str:
        """把 user 画像压成 ~200 字符可读文本,给 system_prompt 当「已知画像」用

        新用户(memory 里没画像)返回 "（暂无画像）",让模板渲染时不会留下空段。
        """
        if not user_id:
            return "（暂无画像）"
        profile = self.memory.get_user_profile_decoded(user_id)
        if not profile:
            return "（暂无画像）"
        parts: list[str] = []
        style = profile.get("style")
        if style:
            parts.append(f"风格:{style}")
        topics = profile.get("topics") or []
        if topics:
            parts.append("关注:" + "/".join(str(t) for t in topics[:5]))
        msg_count = profile.get("message_count")
        if msg_count:
            parts.append(f"已聊过{msg_count}条")
        text = "，".join(parts) if parts else "（暂无画像）"
        return text[:200]

    def _collect_recent_speakers(
        self, window: list[dict], current_user_id: str, bot_user_id: str
    ) -> str:
        """从近期消息窗口里去重提取发言者,按出现顺序

        一定包含当前消息作者(current_user_id)和 bot 自身;其他用户按在 window
        里出现的先后顺序追加。渲染为多行 markdown 列表,空时返回 "（无）"。
        """
        seen: list[tuple[str, str]] = []  # (user_id, username),保持顺序
        seen_ids: set[str] = set()

        def _add(uid: str, uname: str) -> None:
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                seen.append((uid, uname or "?"))

        # 1) 当前作者 + bot 自身 永远在最前
        _add(current_user_id, "")
        _add(bot_user_id, "")

        # 2) 扫 window,补其他发言者
        for m in window:
            _add(m.get("user_id", ""), m.get("username", ""))

        if not seen:
            return "（无）"
        # 补全 username（current_user_id / bot_user_id 在 seen[0..1] 可能是空 username）
        # 用 mm client 兜底查一次,失败就显示 user_id 前 8 位
        rendered = []
        for uid, uname in seen:
            if not uname or uname == "?":
                uname = self.mm.get_username(uid) or uid[:8]
            tag = "（你）" if uid == bot_user_id else ("（当前）" if uid == current_user_id else "")
            rendered.append(f"- @{uname} ({uid[:8]}…){(' ' + tag) if tag else ''}")
        return "\n".join(rendered)

    # 启发式: Mattermost username 命中这些关键词 → 标记为 bot (兜底,db 没 is_bot 字段时用)
    _BOT_USERNAME_HINTS = ("bot", "agent", "test", "system")

    def _classify_role(self, uid: str, username: str) -> str:
        """根据 user_id / db profile / username 给频道成员打角色标签

        优先级:
          1) uid == self.bot_user_id                       → self  (我)
          2) db user_profiles.is_bot == 1                  → bot   (已登记的 bot)
          3) username 命中 bot 关键词启发式                 → bot   (未登记,但 username 像 bot)
          4) 其他                                          → member

        启发式只用于 db 没标记的新 bot。阶段 2 的 db 迁移会给历史 bot 数据
        打 is_bot 标记,长期准确;新 bot 上线后第一次发言会落到启发式分支。
        """
        if uid == self.bot_user_id:
            return "self"
        # db 查 is_bot(若 user_profiles 里有记录)
        try:
            profile = self.memory.get_user_profile(uid) if self.memory else {}
            if profile.get("is_bot"):
                return "bot"
        except Exception:
            # db 还没初始化等异常,fall through 到启发式
            pass
        u = (username or "").lower()
        if any(h in u for h in self._BOT_USERNAME_HINTS):
            return "bot"
        return "member"

    def _build_channel_members_table(
        self, channel_id: str, current_user_id: str
    ) -> str:
        """从频道近期消息里去重提取所有出现过的 user,渲染为结构化 markdown 表格

        解决的问题: 之前只在 system_prompt 里注入「我」和「当前对话者」+「近期发言者」,
        但缺一个稳定的「频道成员坐标系」。结果 hz_bot 那种 system_prompt 里自称
        跟 agent2 一样的 bot 发消息时,我们的 bot 看到「我是 agent2」会误以为对方在说自己。
        显式列出 user_id → role → 自称,LLM 一眼能区分。

        列:
          - user_id(前 8 位,够用就好,减少 token)
          - username
          - role(self / bot / member) — bot 角色用启发式,human 当前是 member

        返回 "（无）" 表示频道还没消息,避免模板里出现空表格。
        """
        window = self.working_memory.get(channel_id, [])
        seen_ids: set[str] = set()
        members: list[tuple[str, str]] = []  # (uid, username),保序

        # 一定包含 bot 自己
        if self.bot_user_id:
            seen_ids.add(self.bot_user_id)
            members.append((self.bot_user_id, ""))

        # 扫 window 补其他成员
        for m in window:
            uid = m.get("user_id", "")
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                members.append((uid, m.get("username", "")))

        if not members or (len(members) == 1 and members[0][0] == self.bot_user_id):
            # 频道还没有过人类/其他 bot 发言,只有 bot 自己 — 渲染成空表格没意义
            return "（无）"

        # 渲染为表格
        lines = ["| uid(前 8) | username | role | 备注 |", "|---|---|---|---|"]
        for uid, uname in members:
            if not uname or uname == "?":
                uname = self.mm.get_username(uid) or uid[:8]
            role = self._classify_role(uid, uname)
            # 备注: 当前对话者 / bot 自称风险提示
            note = ""
            if uid == current_user_id:
                note = "当前对话者"
            elif role == "bot":
                note = "其他 bot,system_prompt 里的'自称'不一定代表真实身份"
            lines.append(
                f"| {uid[:8]}… | @{uname} | {role} | {note} |"
            )
        return "\n".join(lines)

    def _build_context(self, post: dict, mention: bool = False) -> dict:
        """构建 LLM 上下文（受 max_context_messages + max_context_chars 双重限制）

        关键点: 把当前频道的 ID + name 注入到 user 当前消息前缀，
        这样 LLM 调 get_posts / save_knowledge 等需要 channel_id 的工具时，
        就能拿到准确的 ID（而不是猜 name）。
        """
        channel_id = post["channel_id"]
        ch_info = self.mm.get_channel(channel_id)
        ch_name = ch_info.get("display_name", channel_id[:8])
        ch_real_name = ch_info.get("name", "")  # 频道短名 (e.g. "general")

        # 对话者信息(给 system_prompt 注入用)
        current_user_id = post.get("user_id", "")
        current_user_username = post.get("username", "?")
        current_user_profile = self._format_user_profile_summary(current_user_id)
        speaker_window = self.working_memory.get(channel_id, [])
        recent_speakers = self._collect_recent_speakers(
            speaker_window, current_user_id, self.bot_user_id
        )
        channel_members = self._build_channel_members_table(
            channel_id, current_user_id
        )

        # 系统提示词（纯人格，不包含工具信息 — 工具通过 SDK tools 参数传递）
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")
        system = prompts.get(
            "system_prompt",
            bot_username=self.bot_username,
            bot_user_id=self.bot_user_id,
            current_user_id=current_user_id,
            current_user_username=current_user_username,
            current_user_profile=current_user_profile,
            recent_speakers=recent_speakers,
            channel_members=channel_members,
            current_time=current_time,
        )

        # 消息历史
        window = self.working_memory.get(channel_id, [])
        recent = window[-config.max_context_messages :]

        # 格式化为 LLM messages
        messages: list[dict[str, Any]] = []
        prev_ts_ms: float | None = None
        for m in recent:
            role = "assistant" if m.get("user_id") == self.bot_user_id else "user"
            name = m.get("username", "?")
            raw_content = m.get("message", "")
            ts_ms = m.get("create_at") or 0
            # 跨天才补 MM-DD,同一天只显示 HH:MM
            time_label = _format_time_label(ts_ms, prev_ts_ms)
            prev_ts_ms = ts_ms if ts_ms else prev_ts_ms
            if role == "assistant":
                content = f"{time_label} {raw_content}"
            else:
                content = f"{time_label} {name}: {raw_content}"
            messages.append({"role": role, "content": content})

        # ── 当前消息前缀：注入频道上下文，让 LLM 调工具时知道传哪个 ID ──
        meta_lines = [f"📍 频道: {ch_name} | id={channel_id} | name={ch_real_name}"]
        summary = self.memory.get_recent_summary(channel_id)
        if summary:
            meta_lines.append(f"📝 最近讨论摘要: {summary}")
        knowledge = self.memory.get_relevant_knowledge(channel_id, post["message"], 3)
        if knowledge:
            kb_text = "\n".join(f"  - {k['key']}: {k['value']}" for k in knowledge)
            meta_lines.append(f"📚 相关团队知识:\n{kb_text}")
        # 第一行（频道信息）同行，其余换行缩进
        meta_prefix = "[" + meta_lines[0] + ("\n" + "\n".join(meta_lines[1:]) if len(meta_lines) > 1 else "") + "]\n"

        # 加入当前消息（带频道元信息前缀）
        # 多模态: 如果 _on_posted 已下载图片, 用 list[ContentBlock] 注入;
        #        否则走纯文本路径(向后兼容)
        text_payload = f"{meta_prefix}{post['username']}: {post['message']}"
        image_blocks = post.get("_llm_content_blocks")
        if image_blocks:
            # Anthropic content 列表: image blocks 在前, 文本块在后
            current_user_content: str | list = list(image_blocks) + [
                {"type": "text", "text": text_payload}
            ]
        else:
            current_user_content = text_payload
        messages.append({"role": "user", "content": current_user_content})

        # ── 总字符上限裁剪：从旧消息开始丢弃，保留当前消息 ──
        max_chars = config.max_context_chars
        if max_chars > 0:

            def _msg_chars(m: dict) -> int:
                """计算单条 message 的字符当量:
                - 纯文本 content: 直接 len
                - list[ContentBlock]: text 块累加 len(text), image 块按 1500 token 估算字符
                """
                c = m.get("content")
                if isinstance(c, str):
                    return len(c)
                if isinstance(c, list):
                    total = 0
                    for blk in c:
                        if isinstance(blk, dict):
                            if blk.get("type") == "text":
                                total += len(blk.get("text", ""))
                            elif blk.get("type") == "image":
                                total += 6000  # 一张图 ≈ 1500 token ≈ 6000 字符
                    return total
                return 0

            total_chars = sum(_msg_chars(m) for m in messages)
            if total_chars > max_chars:
                # 始终保留最后一条（当前消息），从头部裁剪
                current_msg = messages.pop()
                while len(messages) > 1 and sum(
                    _msg_chars(m) for m in messages
                ) > max_chars - _msg_chars(current_msg):
                    messages.pop(0)
                messages.append(current_msg)
                log.debug(
                    "[上下文] 字符裁剪: %d → %d (上限 %d)",
                    total_chars,
                    sum(_msg_chars(m) for m in messages),
                    max_chars,
                )

        return {"system": system, "messages": messages}

    async def typing_indicator(self, channel_id: str, duration: float | None = None):
        """模拟打字指示器 (通过延迟发送来模拟)"""
        if duration is None:
            duration = config.typing_delay_min + random.random() * (
                config.typing_delay_max - config.typing_delay_min
            )
        await asyncio.sleep(duration)

    async def _send_get_ack(self, post: dict):
        """发送 "get" 线程回执 — 让用户知道已收到、正在处理

        - 以 thread reply 发送 (root_id = 原消息 id),不占主聊天流
        - 不记入 message_log / stats,不污染 LLM 上下文
        """
        try:
            self.mm.send_post(
                channel_id=post["channel_id"],
                message="get",
                root_id=post.get("id", ""),
                props={PROP_FROM_BOT: PROP_TRUE},
            )
        except Exception as e:
            log.debug("get ack 发送失败: %s", e)

    async def _typing_loop(self, channel_id: str):
        """持续发送 typing indicator,直到被 cancel

        Mattermost typing indicator ~3s 过期,每 2.5s 重发。
        """
        try:
            while True:
                self.mm.send_typing(channel_id)
                await asyncio.sleep(2.5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("typing loop 异常: %s", e)

    async def reply(self, post: dict, message: str) -> str | None:
        """发送消息到频道 (主聊天流，非线程)，返回 post_id 或 None"""
        if not message:
            log.warning("       reply(): 消息为空，跳过发送")
            return None

        # 不传 root_id → 消息直接出现在主聊天流，而不是线程(Threads)
        # 如果需要线程回复（如长文分析），可手动指定 root_id
        try:
            post_id = self.mm.send_post(
                channel_id=post["channel_id"],
                message=message,
                props={PROP_FROM_BOT: PROP_TRUE},
            )
            if post_id:
                self.stats["responses"] += 1
                # 把自己的回复也缓存起来
                if not self.memory.log_message(
                    {
                        "id": post_id or "",
                        "channel_id": post["channel_id"],
                        "user_id": self.bot_user_id,
                        "username": self.bot_username,
                        "message": message,
                        "create_at": int(time.time() * 1000),
                        "type": "",
                        "root_id": "",  # 主流消息无 root_id
                    }
                ):
                    self.stats["dropped_messages"] += 1
                return post_id
            else:
                log.error(f"       ❌ send_post 返回 None! channel={post['channel_id'][:8]}")
                return None
        except Exception as e:
            log.error(f"       ❌ send_post 异常: {e}")
            return None

    async def stop(self):
        """停止 Agent — 按顺序释放资源,任一失败不影响后续清理"""
        self.running = False

        # 1) 关闭 WebSocket (解除 ws.run() 阻塞)
        if getattr(self, "ws", None) is not None:
            try:
                await self.ws.close()
            except Exception as e:
                log.error("ws.close 失败: %s", e, exc_info=True)

        # 1.5) 关闭 SDK LLM 持久连接
        if hasattr(self, "sdk_llm"):
            try:
                await self.sdk_llm.stop()
            except Exception as e:
                log.error("sdk_llm.stop 失败: %s", e, exc_info=True)

        # 2) 关闭 MCP 外部连接 (会注销注入的 mcp_* 工具)
        if hasattr(self, "mcp_bridge"):
            try:
                await self.mcp_bridge.close_all()
            except Exception as e:
                log.error("mcp_bridge.close_all 失败: %s", e, exc_info=True)

        # 3) 关闭 url_analyzer 的全局 httpx 客户端 (避免 Agent 退出后残留连接池)
        try:
            from .url_analyzer import close_client

            await close_client()
        except Exception as e:
            log.error("url_analyzer.close_client 失败: %s", e, exc_info=True)

        # 4) 关闭 SQLite 连接 (最后 — 前面步骤可能还需要读 message_log)
        if hasattr(self, "memory"):
            try:
                self.memory.close()
            except Exception as e:
                log.error("memory.close 失败: %s", e, exc_info=True)

        log.info("Agent 已停止")


# ============================================================
# 内部辅助函数
# ============================================================


def _format_time_label(ts_ms: float, prev_ts_ms: float | None) -> str:
    """为单条消息生成紧凑的时间前缀标签,让 LLM 看到"消息间隔"

    规则:
      - 缺时间戳 (ts_ms=0) → 返回空串,不加前缀(不影响老数据)
      - 第一条消息 (prev_ts_ms=None) → 总是带 MM-DD,让 LLM 有"起点日期"
      - 跟上一条同一天 → 只显示 HH:MM
      - 跟上一条跨天 → 补 MM-DD HH:MM
    """
    if not ts_ms:
        return ""
    cur = datetime.fromtimestamp(ts_ms / 1000)
    if prev_ts_ms is None:
        return f"[{cur:%m-%d %H:%M}]"
    prev = datetime.fromtimestamp(prev_ts_ms / 1000)
    if cur.date() != prev.date():
        return f"[{cur:%m-%d %H:%M}]"
    return f"[{cur:%H:%M}]"
