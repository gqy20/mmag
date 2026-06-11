"""
核心 Agent — 消息处理 + 响应编排 (支持 Agentic Tool Use)

WebSocket 连接管理已拆分到 ws_client.py, 记忆压缩拆分到 memory_compactor.py
"""

import asyncio
import json
import random
import time

from .client import MMClient
from .config import _log_config_loading, config
from .llm import LLM
from .logger import get_logger, trace
from .mcp_bridge import MCPClientBridge
from .memory import Memory
from .memory_compactor import MemoryCompactor
from .prompts import prompts
from .tools import ToolRegistry, build_builtin_tools
from .ws_client import WebSocketClient

log = get_logger(__name__)


class Agent:
    """Mattermost AI Agent 主类 — 支持 Agentic Tool Use"""

    def __init__(self):
        self.config = config
        self.mm = MMClient()
        self.llm = LLM()
        self.memory = Memory(config.memory_db_path)

        # 工具注册系统
        self.tool_registry = ToolRegistry()
        builtin_tools = build_builtin_tools(self.mm, self.memory)
        for t in builtin_tools:
            self.tool_registry.register(t)
        log.info(f"工具系统就绪: {len(builtin_tools)} 个内置工具")

        # MCP 外部工具桥接（读取 .mcp.json，连接外部 Server）
        self.mcp_bridge = MCPClientBridge(self.tool_registry)

        # 运行状态
        self.start_time = time.time()
        self.stats = {"messages": 0, "responses": 0, "errors": 0}
        self.running = False

        # 频道级状态
        self.working_memory: dict[str, list] = {}  # 频道 → 最近消息窗口

        # Bot 身份 (启动时获取)
        self.bot_user_id = ""
        self.bot_username = ""

        # 记忆压缩器 (长期记忆层管理)
        self.compactor = MemoryCompactor(
            memory=self.memory,
            llm=self.llm,
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
        log.info(f"  🤖 {config.bot_display_name} Agent 启动中...")
        log.info("=" * 50)

        # 阶段 1: 获取 Bot 身份
        log.info("[1/5] 获取 Bot 身份...")
        me = self.mm.get_me()
        self.bot_user_id = me["id"]
        self.bot_username = me["username"]
        if config.mm_bot_user_id:
            self.bot_user_id = config.mm_bot_user_id
        log.info(f"       ✅ Bot: @{self.bot_username} ({self.bot_user_id})")

        # 阶段 2: LLM 配置检查
        log.info("[2/5] 检查 LLM 配置...")
        log.info(f"       模型: {config.anthropic_model}")
        log.info(f"       API:  {config.anthropic_base_url or '默认 (api.anthropic.com)'}")
        key_preview = (
            f"{config.anthropic_api_key[:8]}...{config.anthropic_api_key[-4:]}"
            if config.anthropic_api_key
            else "(未设置!)"
        )
        log.info(f"       Key:  {key_preview}")
        if not config.anthropic_api_key:
            log.error("       ❌ ANTHROPIC_API_KEY 未设置! 请检查 .env")
            self.running = False
            return

        # 阶段 2.5: 连接 MCP 外部工具 Server
        log.info("[2.5/5] 加载 MCP 外部工具...")
        try:
            mcp_count = await self.mcp_bridge.load_and_connect()
            if mcp_count > 0:
                total_mcp_tools = sum(
                    1 for t in self.tool_registry.list_tools() if t.name.startswith("mcp_")
                )
                log.info(
                    f"       ✅ MCP 已连接 {mcp_count} 个 Server, "
                    f"注册 {total_mcp_tools} 个外部工具"
                )
            else:
                log.info("       ⏭️ 无 MCP 配置 (.mcp.json 不存在或为空)")
        except Exception as e:
            log.warning("       ⚠️ MCP 加载失败（不影响运行）: %s", e)

        # 阶段 3: 预加载频道消息到缓存
        log.info("[3/5] 预加载频道消息...")
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
            for ch in channels_to_load:
                ch_id = ch["id"]
                posts = self.mm.get_posts(ch_id, limit=config.max_context_messages)
                self.working_memory[ch_id] = []
                for p in posts:
                    self.memory.cache_message(p)
                    p["username"] = self.mm.get_username(p.get("user_id", ""))
                    self.working_memory[ch_id].append(p)
                total_msgs += len(posts)
                log.info(
                    f"       📂 {ch.get('display_name', ch_id[:8]):<15} {len(posts):>3} 条消息"
                )
            if channels_to_load:
                log.info(f"       ✅ 共 {len(channels_to_load)} 个频道, {total_msgs} 条消息已缓存")
        except Exception as e:
            log.warning(f"       ⚠️ 预加载频道失败: {e}")
            log.warning("       (不影响运行，将使用空上下文启动)")

        # 阶段 4+5: 启动 WebSocket 客户端 (封装了连接/握手/心跳/重连)
        log.info("[4/5] 建立 WebSocket 连接 (官方协议)...")
        self.running = True  # 必须在进入循环前设置!

        self.ws = WebSocketClient(
            url=config.ws_url,
            token=config.mm_token,
            on_event=self._on_ws_event,
            on_response=self._on_ws_response,
        )

        # 阶段 5: 提示就绪
        log.info("[5/5] 🎯 进入事件监听循环...")
        log.info(f"       旁听概率: {config.listen_probability:.0%}")
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

        data.post 的格式有三种可能 (官方源码 webSocketEventJSON):
          1. 完整 dict 对象 (Python json.loads 后)
          2. JSON 字符串 (包含完整 post 对象，需二次解析)
          3. 纯 post ID 字符串 (旧版或精简模式，需 REST API 补全)
        """
        data = event.get("data", {})
        raw_post = data.get("post")

        if not raw_post:
            return

        # 统一为 dict
        if isinstance(raw_post, dict):
            post = raw_post
        elif isinstance(raw_post, str):
            # 尝试解析为 JSON (完整 post 对象的字符串形式)
            try:
                parsed = json.loads(raw_post)
                if isinstance(parsed, dict) and "id" in parsed and "message" in parsed:
                    post = parsed
                else:
                    # 看起来像纯 ID，通过 REST API 获取
                    post = self.mm._get(f"/posts/{raw_post}")
                    if not post:
                        return
            except (json.JSONDecodeError, Exception):
                # 不是有效 JSON，当作 post ID 处理
                try:
                    post = self.mm._get(f"/posts/{raw_post}")
                    if not post:
                        return
                except Exception as e2:
                    log.warning(f"       无法解析 post ({str(raw_post)[:40]}...): {e2}")
                    return
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
        if not message:
            return

        channel_id = post.get("channel_id", "")
        self.stats["messages"] += 1

        # 补充 username
        post["username"] = self.mm.get_username(user_id)

        # 缓存消息
        self.memory.cache_message(post)

        # Layer 1→2 双阈值管理（定期摘要 + 容量清理）— 委托给 MemoryCompactor
        # 每条消息都检查，compactor 内部有 guard 避免无效操作
        await self.compactor.maybe_compact(channel_id)

        # 更新工作内存
        if channel_id not in self.working_memory:
            self.working_memory[channel_id] = []
        self.working_memory[channel_id].append(post)
        # 保持窗口大小
        if len(self.working_memory[channel_id]) > config.max_context_messages * 2:
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

        # ====== @提及 必回 ======
        bot_mentions = [
            f"@{self.bot_username}",
            f"@{config.bot_name.lower()}",
            f"@{config.bot_display_name.lower()}",
        ]
        if any(m in message.lower() for m in bot_mentions):
            trace.set_context(msg_type="mention")
            log.info("%s → 触发: @提及", trace.prefix())
            await self._respond_to_mention(post)
            trace.clear()
            return

        # ====== DM 私聊必回 ======
        ch_info = self.mm.get_channel(channel_id)
        if ch_info.get("type") == "D":
            trace.set_context(msg_type="dm")
            log.info("%s → 触发: DM 私聊", trace.prefix())
            await self._respond_chat(post)
            trace.clear()
            return

        # ====== 智能旁听 ======
        should = await self._should_respond(post)
        if should:
            trace.set_context(msg_type="listen")
            log.info("%s → 触发: 主动旁听", trace.prefix())
            await self._respond_chat(post)
            trace.clear()

    async def _should_respond(self, post: dict) -> bool:
        """判断是否应该主动回复"""
        message = post["message"]

        # 高概率触发词
        high_trigger = [
            "报错",
            "错误",
            "bug",
            "失败",
            "不行",
            "不行吗",
            "帮忙",
            "帮我看",
            "怎么看",
            "怎么办",
            "为什么",
            "怎么",
            "如何",
            "?",
            "？",
            "部署",
            "上线",
            "发布",
            "回滚",
            "谁知道",
            "有人吗",
            "怎么弄",
            config.bot_name,
            config.bot_display_name,
        ]
        if any(w in message.lower() for w in high_trigger):
            return True

        # 问句检测
        stripped = message.rstrip(" \t。！？.,!?")
        if stripped.endswith(("?", "？", "吗", "呢", "吧")):
            return True

        # 概率旁听
        return random.random() < config.listen_probability

    async def _respond_to_mention(self, post: dict):
        """响应 @提及（支持 Agentic Tool Use）"""
        t0 = time.monotonic()
        log.info("%s [mention] 构建上下文...", trace.prefix())
        await self.typing_indicator(post["channel_id"])
        context = self._build_context(post, mention=True)
        log.info(
            "%s [mention] 调用 Agent Loop (上下文 %d 条消息)...",
            trace.prefix(),
            len(context["messages"]),
        )
        response = await self.llm.agent_loop(
            messages=context["messages"],
            system=context["system"],
            tools=self.tool_registry.get_schema_list(),
            tool_registry=self.tool_registry,
            max_rounds=5,
        )
        elapsed = time.monotonic() - t0
        log.info(
            "%s [mention] Agent Loop 返回 (%.1fs, %d 字符): %s",
            trace.prefix(),
            elapsed,
            len(response),
            response[:150],
        )
        if not response or response.startswith("⚠️"):
            log.warning("%s [mention] LLM 返回异常或为空: %s", trace.prefix(), response[:100])
        else:
            result = await self.reply(post, response)
            log.info("%s [mention] 回复已发送 post_id=%s", trace.prefix(), result)
        self._save_interaction(post, response)

    async def _respond_chat(self, post: dict):
        """响应普通对话/旁听（支持 Agentic Tool Use）"""
        t0 = time.monotonic()
        log.info("%s [chat] 构建上下文...", trace.prefix())
        await self.typing_indicator(post["channel_id"])
        context = self._build_context(post)
        log.info(
            "%s [chat] 调用 Agent Loop (上下文 %d 条消息)...",
            trace.prefix(),
            len(context["messages"]),
        )
        response = await self.llm.agent_loop(
            messages=context["messages"],
            system=context["system"],
            tools=self.tool_registry.get_schema_list(),
            tool_registry=self.tool_registry,
            max_rounds=3,  # 旁听场景限制轮次，避免过度响应
        )
        elapsed = time.monotonic() - t0
        log.info(
            "%s [chat] Agent Loop 返回 (%.1fs, %d 字符): %s",
            trace.prefix(),
            elapsed,
            len(response),
            response[:150],
        )
        if not response or response.startswith("⚠️"):
            log.warning("%s [chat] LLM 返回异常或为空: %s", trace.prefix(), response[:100])
        else:
            result = await self.reply(post, response)
            log.info("%s [chat] 回复已发送 post_id=%s", trace.prefix(), result)
        self._save_interaction(post, response)

    def _build_context(self, post: dict, mention: bool = False) -> dict:
        """构建 LLM 上下文（受 max_context_messages + max_context_chars 双重限制）"""
        channel_id = post["channel_id"]
        ch_info = self.mm.get_channel(channel_id)
        ch_name = ch_info.get("display_name", channel_id[:8])

        # 系统提示词（纯人格，不包含工具信息 — 工具通过 SDK tools 参数传递）
        system = prompts.get(
            "system_prompt",
            bot_name=config.bot_display_name,
            bot_username=self.bot_username,
        )

        # 消息历史
        window = self.working_memory.get(channel_id, [])
        recent = window[-config.max_context_messages :]

        # 格式化为 LLM messages
        messages = []
        for m in recent:
            role = "assistant" if m.get("user_id") == self.bot_user_id else "user"
            name = m.get("username", "?")
            content = m.get("message", "")
            if role == "assistant":
                messages.append({"role": role, "content": content})
            else:
                messages.append(
                    {
                        "role": role,
                        "content": f"{name}: {content}",
                    }
                )

        # 加入当前消息
        messages.append(
            {
                "role": "user",
                "content": f"{post['username']}: {post['message']}",
            }
        )

        # ── 总字符上限裁剪：从旧消息开始丢弃，保留当前消息 ──
        max_chars = config.max_context_chars
        if max_chars > 0:
            total_chars = sum(len(m["content"]) for m in messages)
            if total_chars > max_chars:
                # 始终保留最后一条（当前消息），从头部裁剪
                current_msg = messages.pop()
                while len(messages) > 1 and sum(
                    len(m["content"]) for m in messages
                ) > max_chars - len(current_msg["content"]):
                    messages.pop(0)
                messages.append(current_msg)
                log.debug(
                    "[上下文] 字符裁剪: %d → %d (上限 %d)",
                    total_chars,
                    sum(len(m["content"]) for m in messages),
                    max_chars,
                )

        # 额外上下文
        channel_ctx = f"当前频道: {ch_name}"
        summary = self.memory.get_recent_summary(channel_id)
        if summary:
            channel_ctx += f"\n最近讨论摘要: {summary}"

        knowledge = self.memory.get_relevant_knowledge(channel_id, post["message"], 3)
        if knowledge:
            kb_text = "\n".join(f"- {k['key']}: {k['value']}" for k in knowledge)
            channel_ctx += f"\n相关团队知识:\n{kb_text}"

        return {"system": system, "messages": messages, "channel_context": channel_ctx}

    async def typing_indicator(self, channel_id: str, duration: float | None = None):
        """模拟打字指示器 (通过延迟发送来模拟)"""
        if duration is None:
            duration = config.typing_delay_min + random.random() * (
                config.typing_delay_max - config.typing_delay_min
            )
        await asyncio.sleep(duration)

    async def reply(self, post: dict, message: str) -> str | None:
        """发送消息到频道 (主聊天流，非线程)，返回 post_id 或 None"""
        if not message:
            log.warning("       reply(): 消息为空，跳过发送")
            return None
        if message.startswith("⚠️"):
            log.warning(f"       reply(): LLM 错误响应，不发送: {message[:80]}")
            return None

        # 不传 root_id → 消息直接出现在主聊天流，而不是线程(Threads)
        # 如果需要线程回复（如长文分析），可手动指定 root_id
        try:
            post_id = self.mm.send_post(
                channel_id=post["channel_id"],
                message=message,
                props={"from_bot": "true"},
            )
            if post_id:
                self.stats["responses"] += 1
                # 把自己的回复也缓存起来
                self.memory.cache_message(
                    {
                        "id": post_id or "",
                        "channel_id": post["channel_id"],
                        "user_id": self.bot_user_id,
                        "message": message,
                        "create_at": int(time.time() * 1000),
                        "type": "",
                        "root_id": "",  # 主流消息无 root_id
                    }
                )
                return post_id
            else:
                log.error(f"       ❌ send_post 返回 None! channel={post['channel_id'][:8]}")
                return None
        except Exception as e:
            log.error(f"       ❌ send_post 异常: {e}")
            return None

    async def ephemeral(self, post: dict, message: str):
        """发送仅触发者可见的消息"""
        self.mm.send_ephemeral(post["user_id"], post["channel_id"], message)

    def _save_interaction(self, post: dict, response: str):
        """异步保存交互到记忆 (不阻塞主流程)"""
        # 定期提取记忆 (每 20 条对话)
        if self.stats["responses"] % 20 == 0:
            channel_id = post["channel_id"]
            window = self.working_memory.get(channel_id, [])
            if len(window) >= 10:
                log.info(
                    "%s [memory] 到达知识提取阈值 (总回复=%d, 窗口=%d条)",
                    trace.prefix(),
                    self.stats["responses"],
                    len(window),
                )
                texts = [
                    f"{m.get('username', '?')}: {m.get('message', '')[:200]}" for m in window[-15:]
                ]
                # 后续可以在这里调用 LLM 提取知识
                log.debug("%s [memory] 待提取文本预览: %s", trace.prefix(), "; ".join(texts[:3]))

    async def stop(self):
        """停止 Agent"""
        self.running = False
        # 关闭 WebSocket (解除 ws.run() 阻塞)
        if getattr(self, "ws", None) is not None:
            await self.ws.close()
        # 关闭 MCP 外部连接
        if hasattr(self, "mcp_bridge"):
            await self.mcp_bridge.close_all()
        self.memory.close()
        log.info("Agent 已停止")
