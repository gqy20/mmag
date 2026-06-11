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
from .llm import LLM, LLMError
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
        self.stats = {"messages": 0, "responses": 0}
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
        if not message:
            return

        self.stats["messages"] += 1

        # 补充 username
        post["username"] = self.mm.get_username(user_id)

        # 缓存消息
        self.memory.log_message(post)

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

        # ====== @提及 必回 ======
        bot_mentions = [
            f"@{self.bot_username}",
            f"@{config.bot_name.lower()}",
            f"@{config.bot_display_name.lower()}",
        ]
        if any(m in message.lower() for m in bot_mentions):
            trace.set_context(msg_type="mention")
            log.info("%s → 触发: @提及", trace.prefix())
            await self._respond(post, tag="mention", max_rounds=5)
            trace.clear()
            return

        # ====== DM 私聊必回 ======
        ch_info = self.mm.get_channel(channel_id)
        if ch_info.get("type") == "D":
            trace.set_context(msg_type="dm")
            log.info("%s → 触发: DM 私聊", trace.prefix())
            await self._respond(post, tag="chat", max_rounds=3)
            trace.clear()
            return

        # ====== 智能旁听 ======
        should = await self._should_respond(post)
        if should:
            trace.set_context(msg_type="listen")
            log.info("%s → 触发: 主动旁听", trace.prefix())
            await self._respond(post, tag="chat", max_rounds=3)
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

    async def _respond(self, post: dict, *, tag: str, max_rounds: int):
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
        log.info(
            "%s [%s] 调用 Agent Loop (上下文 %d 条消息)...",
            trace.prefix(),
            tag,
            len(context["messages"]),
        )
        try:
            response = await self.llm.agent_loop(
                messages=context["messages"],
                system=context["system"],
                tools=self.tool_registry.get_schema_list(),
                tool_registry=self.tool_registry,
                max_rounds=max_rounds,
            )
        except LLMError as e:
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
        messages.append(
            {
                "role": "user",
                "content": f"{meta_prefix}{post['username']}: {post['message']}",
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

        return {"system": system, "messages": messages}

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
                self.memory.log_message(
                    {
                        "id": post_id or "",
                        "channel_id": post["channel_id"],
                        "user_id": self.bot_user_id,
                        "username": config.bot_display_name,
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
