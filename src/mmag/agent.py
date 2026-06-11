"""
核心 Agent — WebSocket 事件循环 + 消息处理 (支持 Agentic Tool Use)
"""

import asyncio
import contextlib
import json
import random
import time

import websockets

from .client import MMClient
from .config import _log_config_loading, config
from .llm import LLM
from .logger import get_logger, trace
from .memory import Memory
from .prompts import prompts
from .tools import ToolRegistry, build_builtin_tools

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

        # 运行状态
        self.start_time = time.time()
        self.stats = {"messages": 0, "responses": 0, "errors": 0}
        self.running = False

        # 频道级状态
        self.working_memory: dict[str, list] = {}  # 频道 → 最近消息窗口

        # Bot 身份 (启动时获取)
        self.bot_user_id = ""
        self.bot_username = ""

    # ---- 官方 WebSocket 协议状态 ----
    _conn_id: str = ""  # 服务端分配的 connection_id (用于断线续传)
    _server_seq: int = 0  # 服务端事件序列号 (用于检测丢包)
    _client_seq: int = 1  # 客户端请求序列号
    _connect_fail_count: int = 0  # 连接失败计数 (用于指数退避)
    _last_err_code: str | None = None  # 上次断开错误码
    _ping_task: asyncio.Task | None = None  # 心跳任务

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

        # 阶段 4+5: WebSocket 连接 + 事件循环 (官方协议)
        log.info("[4/5] 建立 WebSocket 连接 (官方协议)...")
        self.running = True  # 必须在进入循环前设置!

        # 重连参数 (参考 webapp/platform/client/src/websocket.ts)
        min_retry_s = 3  # 最小重试间隔
        max_retry_s = 300  # 最大重试间隔 (5分钟)
        jitter_range_s = 2  # 抖动范围
        max_fails_before_backoff = 7  # 超过此次数开始指数退避

        while self.running:
            try:
                # 构建带断线续传参数的 URL (官方做法)
                ws_url = config.ws_url
                params = []
                if self._conn_id:
                    params.append(f"connection_id={self._conn_id}")
                    params.append(f"sequence_number={self._server_seq}")
                if self._last_err_code:
                    params.append(f"disconnect_err_code={self._last_err_code}")
                if params:
                    ws_url += "?" + "&".join(params)

                log.info(f"       → {ws_url[:80]}{'...' if len(ws_url) > 80 else ''}")

                async with websockets.connect(
                    ws_url,
                    additional_headers={"Authorization": f"Bearer {config.mm_token}"},
                    open_timeout=10,
                    ping_interval=None,  # 我们自己管理心跳
                    ping_timeout=None,
                ) as ws:
                    log.info("       ✅ WebSocket 已连接")

                    # ── 官方握手流程 ──
                    # Step 1: 收 Hello (服务器首条推送)
                    hello = json.loads(await ws.recv())
                    if hello.get("event") != "hello":
                        log.warning(f"       ⚠️ 首条非 hello: {hello.get('event', '?')}")

                    new_conn_id = hello.get("data", {}).get("connection_id", "")
                    server_ver = hello.get("data", {}).get("server_version", "?")

                    # 如果 conn_id 变了说明是长时间断线或服务端重启
                    if self._conn_id and self._conn_id != new_conn_id:
                        log.warning("       ⚠️ connection_id 变化，可能有遗漏消息")

                    self._conn_id = new_conn_id
                    # 官方做法: hello 的 seq 之后紧接着就是下一个事件
                    self._server_seq = hello.get("seq", 0) + 1
                    self._client_seq = 1
                    self._connect_fail_count = 0
                    self._last_err_code = None
                    log.info(f"       📨 Hello | id={self._conn_id[:12]}... v{server_ver}")

                    # Step 2: 发送认证 (官方: onopen 时立即发)
                    auth_msg = json.dumps(
                        {
                            "action": "authentication_challenge",
                            "seq": self._client_seq,
                            "data": {"token": config.mm_token},
                        }
                    )
                    self._client_seq += 1
                    await ws.send(auth_msg)
                    log.info("       🔑 认证请求已发送")

                    # Step 3: 启动心跳 (官方: 每30秒 ping)
                    self._ping_task = asyncio.create_task(self._ping_loop(ws), name="ws-ping")

                    # 阶段 5: 进入事件循环
                    log.info("[5/5] 🎯 进入事件监听循环...")
                    log.info(f"       旁听概率: {config.listen_probability:.0%}")
                    log.info(f"       上下文窗口: {config.max_context_messages} 条")
                    log.info("       纯自然语言驱动，无命令")
                    log.info("")
                    log.info("────── Agent 就绪，等待消息 ──────")

                    try:
                        async for raw_msg in ws:
                            await self._handle_ws_message(raw_msg, ws)
                    except websockets.ConnectionClosed as e:
                        log.warning(f"       🔌 WebSocket 断开: code={e.code}")
                        self._last_err_code = str(e.code)

            except Exception as e:
                log.error(f"       ❌ 连接异常: {e}", exc_info=True)
                self._connect_fail_count += 1
            finally:
                if self._ping_task and not self._ping_task.done():
                    self._ping_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._ping_task

            # ── 指数退避重连 (官方算法) ──
            if not self.running:
                break

            retry_s = min_retry_s
            if self._connect_fail_count > max_fails_before_backoff:
                retry_s = min(min_retry_s * self._connect_fail_count**2, max_retry_s)
            retry_s += random.random() * jitter_range_s  # jitter
            log.info(f"       ⏳ {retry_s:.1f}s 后重连 (第{self._connect_fail_count}次)...")
            await asyncio.sleep(retry_s)

    async def _ping_loop(self, ws):
        """心跳循环 — 参考 webapp 官方实现 (每30秒 ping)"""
        ping_interval_s = 30
        try:
            while True:
                await asyncio.sleep(ping_interval_s)
                ping_msg = json.dumps(
                    {
                        "action": "ping",
                        "seq": self._client_seq,
                    }
                )
                self._client_seq += 1
                await ws.send(ping_msg)
                log.debug("       💓 ping sent")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _compact_channel_cache(self, channel_id: str):
        """Layer 1→Layer 2 双阈值管理

        两个独立动作:
          A. 定期摘要（每 ~100 条消息）: 只读最近一批消息做摘要，
             原始消息保留在 message_cache 中不删除。
          B. 容量清理（超过上限时）: 弹出最旧的 N 条，
             先做摘要再删除，将缓存控制在安全范围。

        摘要完成后会以**线程回复**形式发到频道（不污染主聊天流）。
        """
        cfg = self.config
        summary_interval = cfg.memory_summary_interval
        max_cache_size = cfg.memory_cache_max
        compaction_keep = cfg.memory_compaction_keep
        summary_batch = cfg.memory_summary_batch

        t0 = time.monotonic()
        count = self.memory.get_channel_cache_count(channel_id)

        # ── 动作 A: 定期摘要（每 ~100 条）──
        if count > 0 and count % summary_interval == 0:
            log.info(
                "%s [摘要] channel=%s 第 %d 条 → 触发定期摘要",
                trace.prefix(),
                channel_id[:12],
                count,
            )
            # 取当前批次 + 前序上下文
            peek_count = summary_interval + cfg.memory_context_window
            wider_batch = self.memory.peek_recent_messages(channel_id, limit=peek_count)
            if wider_batch:
                ctx = (
                    wider_batch[:-summary_interval] if len(wider_batch) > summary_interval else None
                )
                recent_batch = wider_batch[-summary_interval:]

                all_summaries = []
                for bs in range(0, len(recent_batch), summary_batch):
                    batch = recent_batch[bs : bs + summary_batch]
                    summary = await self._summarize_message_batch(
                        batch, channel_id, context_messages=ctx
                    )
                    if summary:
                        all_summaries.append(summary)

                if all_summaries:
                    combined = "\n\n---\n\n".join(all_summaries)
                    participants = list(
                        {m.get("username", "?") for m in recent_batch if m.get("username")}
                    )
                    topic = (
                        f"定期摘要 #{count // summary_interval} "
                        f"(msg {count - summary_interval + 1}-{count})"
                    )

                    self.memory.save_conversation_segment(
                        channel_id=channel_id,
                        topic=topic,
                        summary=combined,
                        participants=participants,
                        key_points=[],
                    )

                    # 发送线程回复到频道
                    last_post_id = recent_batch[-1].get("id", "") if recent_batch else ""
                    await self._post_summary_thread(
                        channel_id=channel_id,
                        root_id=last_post_id,
                        title=f"📋 {topic}",
                        content=combined,
                    )

                    elapsed = time.monotonic() - t0
                    log.info(
                        "%s [摘要] 完成 | %d 批 | %.1fs | 缓存仍 %d 条",
                        trace.prefix(),
                        len(all_summaries),
                        elapsed,
                        self.memory.get_channel_cache_count(channel_id),
                    )

        # ── 动作 B: 容量清理（超过上限时）──
        if count > max_cache_size:
            log.info(
                "%s [清理] channel=%s %d 条 > 上限 %d → 开始容量清理",
                trace.prefix(),
                channel_id[:12],
                count,
                max_cache_size,
            )
            old_messages = self.memory.pop_old_messages(channel_id, keep=compaction_keep)
            if not old_messages:
                return

            # 获取紧接在被弹出消息之后的消息作为上下文
            context_msgs = self.memory.peek_recent_messages(
                channel_id, limit=cfg.memory_context_window
            )

            all_summaries = []
            for batch_start in range(0, len(old_messages), summary_batch):
                batch = old_messages[batch_start : batch_start + summary_batch]
                summary = await self._summarize_message_batch(
                    batch, channel_id, context_messages=context_msgs
                )
                if summary:
                    all_summaries.append(summary)

            if all_summaries:
                combined_summary = "\n\n---\n\n".join(all_summaries)
                participants = list(
                    {m.get("username", "?") for m in old_messages if m.get("username")}
                )
                topic = f"容量清理-{time.strftime('%Y-%m-%d_%H%M')} ({len(old_messages)}条)"

                self.memory.save_conversation_segment(
                    channel_id=channel_id,
                    topic=topic,
                    summary=combined_summary,
                    participants=participants,
                    key_points=[],
                )

                # 发送线程回复到频道（挂载在缓存中最新一条消息下）
                latest_msgs = self.memory.peek_recent_messages(channel_id, limit=1)
                anchor_id = latest_msgs[0].get("id", "") if latest_msgs else ""
                await self._post_summary_thread(
                    channel_id=channel_id,
                    root_id=anchor_id,
                    title=f"🗂️ {topic}",
                    content=combined_summary,
                )

                elapsed = time.monotonic() - t0
                remaining = self.memory.get_channel_cache_count(channel_id)
                log.info(
                    "%s [清理] 完成 | 摘要 %d 批 | %.1fs | 缓存剩余 %d 条",
                    trace.prefix(),
                    len(all_summaries),
                    elapsed,
                    remaining,
                )

            if all_summaries:
                combined_summary = "\n\n---\n\n".join(all_summaries)
                participants = list(
                    {m.get("username", "?") for m in old_messages if m.get("username")}
                )
                key_points = []
                for m in old_messages[-20:]:
                    msg = (m.get("message") or "").strip()
                    if len(msg) > 15 and not msg.startswith(("http", "@")):
                        key_points.append(f"[{m.get('username', '?')}] {msg[:120]}")

                self.memory.save_conversation_segment(
                    channel_id=channel_id,
                    topic=f"容量清理-{time.strftime('%Y-%m-%d_%H%M')} ({len(old_messages)}条)",
                    summary=combined_summary,
                    participants=participants,
                    key_points=key_points[-10:],
                )
                elapsed = time.monotonic() - t0
                remaining = self.memory.get_channel_cache_count(channel_id)
                log.info(
                    "%s [清理] 完成 | 摘要 %d 批 | %.1fs | 缓存剩余 %d 条",
                    trace.prefix(),
                    len(all_summaries),
                    elapsed,
                    remaining,
                )

    async def _summarize_message_batch(
        self,
        messages: list[dict],
        channel_id: str,
        context_messages: list[dict] | None = None,
    ) -> str:
        """调用 LLM 对一批消息做结构化摘要（支持注入前序上下文）

        Args:
            messages: 当前要摘要的消息批次
            channel_id: 频道 ID
            context_messages: 前序消息（用于保持上下文连贯），LLM 会参考这些
                             信息来理解当前批次的对话背景，但只对 messages 输出摘要
        """
        cfg = self.config

        # ── 格式化前序上下文（如果有）──
        context_text = ""
        if context_messages:
            ctx_lines = []
            for m in context_messages[-cfg.memory_context_window :]:
                user = m.get("username", "?")
                msg = (m.get("message") or "").strip()[:200]
                ctx_lines.append(f"  {user}: {msg}")
            context_text = (
                "\n【前序对话背景（仅供参考，理解上下文用，不需要重复摘要）】\n"
                + "\n".join(ctx_lines)
                + "\n"
            )

        # ── 格式化当前批次 ──
        lines = []
        for m in messages:
            user = m.get("username", "?")
            msg = (m.get("message") or "").strip()
            ts = m.get("create_at", "")
            time_str = ""
            if ts:
                try:
                    from datetime import datetime

                    dt = datetime.fromtimestamp(ts if ts < 1e12 else ts / 1000.0)
                    time_str = dt.strftime("%H:%M")
                except Exception:
                    pass
            lines.append(f"[{time_str}] {user}: {msg[:300]}")

        conversation_text = "\n".join(lines)

        prompt = (
            "请对以下团队对话记录做简洁的结构化摘要。\n"
            "要求:\n"
            "1. 用 2-3 句话概括这段时间讨论的核心主题和结论\n"
            "2. 如果有明确的决策或行动项，单独列出\n"
            "3. 省略闲聊和无关内容\n"
            "4. 直接输出摘要，不要加前缀说明\n"
            f"{context_text}\n"
            f"\n【当前待摘要对话 ({len(messages)} 条)】\n{conversation_text}"
        )

        try:
            result = await self.llm.chat_with_system(
                system_prompt=(
                    "你是一个对话摘要助手。你的任务是将团队聊天记录压缩为"
                    "精炼的结构化摘要，保留关键信息和决策，去除冗余。"
                    "如果提供了前序对话背景，请用它来理解上下文，但只对"
                    "当前待摘要的对话部分输出摘要。"
                ),
                user_message=prompt,
                max_tokens=1024,
            )
            return result
        except Exception as e:
            log.error("%s [压缩] LLM 摘要失败: %s", trace.prefix(), e)
            return f"(摘要失败: {e})"

    async def _post_summary_thread(self, channel_id: str, root_id: str, title: str, content: str):
        """将摘要以线程回复形式发送到频道

        线程消息不会出现在主聊天流中，用户需要点开才能看到。
        这样既让团队可见知识沉淀，又不打扰正常对话。
        """
        # 截断过长的摘要（Mattermost 单条消息限制 ~4MB，但实际显示体验上 4000 字符为宜）
        max_len = 4000
        if len(content) > max_len:
            content = content[:max_len] + "\n\n...(摘要已截断)"

        message = f"### {title}\n\n{content}"

        try:
            post_id = self.mm.send_post(
                channel_id=channel_id,
                message=message,
                root_id=root_id,
                props={"from_bot": "true", "summary": "true"},
            )
            if post_id:
                log.info(
                    "%s [摘要线程] 已发送 → %s (root=%s)",
                    trace.prefix(),
                    post_id[:12],
                    root_id[:12] if root_id else "(主流)",
                )
            else:
                log.warning("%s [摘要线程] send_post 返回 None", trace.prefix())
        except Exception as e:
            log.error("%s [摘要线程] 发送失败: %s", trace.prefix(), e)

    async def _handle_ws_message(self, raw: str, ws):
        """
        处理单条 WebSocket 消息。

        官方协议区分两种消息类型 (见 websocket_client.go Listen()):
          1. **Event** (服务端推送): 有 `event` 字段，带递增 `seq`
          2. **Response** (请求回复): 有 `seq_reply` 字段，有 `status`

        客户端必须校验序列号，发现不连续则说明丢包。
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug(f"       ⚠️ 无法解析 JSON: {raw[:80]}")
            return

        # ── 区分 Response vs Event (官方核心逻辑) ──
        if "seq_reply" in msg:
            # 这是某个请求的响应 (如认证结果、ping 回复等)
            self._on_response(msg)
            return

        # 这是服务端事件流
        etype = msg.get("event", "")

        # ── 序列号校验 (官方: 发现不连续则重连) ──
        msg_seq = msg.get("seq", -1)
        if msg_seq != self._server_seq:
            log.warning(
                f"       ⚠️ 序列号不连续! 期望={self._server_seq} 实际={msg_seq}"
                f" (可能丢失 {msg_seq - self._server_seq} 条事件)"
            )
            # 注意: 不直接断开，而是记录并继续（Bot 场景容忍少量丢包）
        self._server_seq = msg_seq + 1

        # ── 分发事件 ──
        async def _noop(_m, _w):
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
            await handler(msg, ws)
        elif etype not in ("hello",):
            log.debug(f"       [未处理事件] {etype}")

    def _on_response(self, msg: dict):
        """处理 WebSocket 响应 (认证结果等)"""
        seq_reply = msg.get("seq_reply", 0)
        status = msg.get("status", "?")
        err = msg.get("error")

        if err:
            log.error(f"       ❌ 响应错误 (seq={seq_reply}): {err}")
        elif status == "OK":
            log.debug(f"       ✅ 响应 OK (seq={seq_reply})")
        else:
            log.debug(f"       响应 (seq={seq_reply}): status={status}")

    async def _on_posted(self, event: dict, ws):
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

        # Layer 1→2 双阈值管理（定期摘要 + 容量清理）
        # 每条消息都检查，内部有 guard 避免无效操作
        count = self.memory.get_channel_cache_count(channel_id)
        if count > 0 and (
            count % config.memory_summary_interval == 0 or count > config.memory_cache_max
        ):
            await self._compact_channel_cache(channel_id)

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
        """构建 LLM 上下文"""
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
        self.memory.close()
        log.info("Agent 已停止")
