"""
记忆压缩器 — 长期记忆层 (Layer 1→Layer 2) 管理

负责:
  - 定期摘要 (每 N 条消息触发一次, 原始消息保留)
  - 容量清理 (缓存超过上限时弹出旧消息, 先摘要再删)
  - 摘要结果以**线程回复**形式发到频道 (不污染主流)

设计动机 (从 agent.py 拆出):
  - 摘要/清理是异步后台任务, 不该阻塞消息处理
  - 涉及 LLM 调用、数据库写入、Mattermost 发帖三套逻辑, 单独成类更易测试
  - 修复原代码中 319-345 行的重复执行 bug (同一份摘要会保存两次到 segment)

触发方式:
  - agent._on_posted 每条消息后调一次 maybe_compact(channel_id)
  - 内部按阈值判断是否真要执行 (避免每次都进 LLM)
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from .logger import get_logger, trace

if TYPE_CHECKING:
    from .client import MMClient
    from .config import Config
    from .llm import LLM
    from .memory import Memory

log = get_logger(__name__)


# 摘要线程展示层字符上限 (超过则截断, 避免 Mattermost 单条消息过大)
_THREAD_MAX_CHARS = 4000


class MemoryCompactor:
    """长期记忆压缩器 (Layer 1→Layer 2)

    Args:
        memory: Memory 实例 (负责 SQLite 读写)
        llm: LLM 实例 (负责摘要生成)
        mm_client: MMClient 实例 (负责发摘要线程回复)
        config: 全局配置
    """

    def __init__(
        self,
        memory: Memory,
        llm: LLM,
        mm_client: MMClient,
        config: Config,
    ) -> None:
        self.memory = memory
        self.llm = llm
        self.mm = mm_client
        self.config = config

    # ============================================================
    # 公共入口
    # ============================================================

    async def maybe_compact(self, channel_id: str) -> None:
        """每条消息后调用 — 内部按阈值判断是否触发摘要/清理

        触发条件 (任一满足):
          - 消息总数 % summary_interval == 0  →  定期摘要
          - 消息总数 > cache_max               →  容量清理
        """
        count = self.memory.get_channel_cache_count(channel_id)
        if count <= 0:
            return

        needs_summary = count % self.config.memory_summary_interval == 0
        needs_cleanup = count > self.config.memory_cache_max

        if needs_summary:
            await self._periodic_summary(channel_id, count)
        if needs_cleanup:
            await self._capacity_cleanup(channel_id, count)

    # ============================================================
    # 动作 A: 定期摘要
    # ============================================================

    async def _periodic_summary(self, channel_id: str, count: int) -> None:
        """每 ~summary_interval 条消息触发一次 (不删原消息)"""
        summary_interval = self.config.memory_summary_interval
        summary_batch = self.config.memory_summary_batch

        log.info(
            "%s [摘要] channel=%s 第 %d 条 → 触发定期摘要",
            trace.prefix(),
            channel_id[:12],
            count,
        )

        t0 = time.monotonic()

        # 取当前批次 + 前序上下文 (帮助 LLM 理解连贯性)
        peek_count = summary_interval + self.config.memory_context_window
        wider_batch = self.memory.peek_recent_messages(channel_id, limit=peek_count)
        if not wider_batch:
            return

        ctx = wider_batch[:-summary_interval] if len(wider_batch) > summary_interval else None
        recent_batch = wider_batch[-summary_interval:]

        all_summaries = []
        for bs in range(0, len(recent_batch), summary_batch):
            batch = recent_batch[bs : bs + summary_batch]
            summary = await self._summarize_message_batch(batch, channel_id, context_messages=ctx)
            if summary:
                all_summaries.append(summary)

        if not all_summaries:
            return

        combined = "\n\n---\n\n".join(all_summaries)
        participants = list({m.get("username", "?") for m in recent_batch if m.get("username")})
        topic = (
            f"定期摘要 #{count // summary_interval} (msg {count - summary_interval + 1}-{count})"
        )

        # 持久化
        self.memory.save_conversation_segment(
            channel_id=channel_id,
            topic=topic,
            summary=combined,
            participants=participants,
            key_points=[],
        )

        # 线程回复 (不污染主流)
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

    # ============================================================
    # 动作 B: 容量清理
    # ============================================================

    async def _capacity_cleanup(self, channel_id: str, count: int) -> None:
        """缓存超过上限时弹出旧消息 (先摘要再删)"""
        max_cache_size = self.config.memory_cache_max
        compaction_keep = self.config.memory_compaction_keep
        summary_batch = self.config.memory_summary_batch

        log.info(
            "%s [清理] channel=%s %d 条 > 上限 %d → 开始容量清理",
            trace.prefix(),
            channel_id[:12],
            count,
            max_cache_size,
        )

        t0 = time.monotonic()

        old_messages = self.memory.pop_old_messages(channel_id, keep=compaction_keep)
        if not old_messages:
            return

        # 获取紧接在被弹出消息之后的消息作为上下文
        context_msgs = self.memory.peek_recent_messages(
            channel_id, limit=self.config.memory_context_window
        )

        all_summaries = []
        for batch_start in range(0, len(old_messages), summary_batch):
            batch = old_messages[batch_start : batch_start + summary_batch]
            summary = await self._summarize_message_batch(
                batch, channel_id, context_messages=context_msgs
            )
            if summary:
                all_summaries.append(summary)

        if not all_summaries:
            return

        combined_summary = "\n\n---\n\n".join(all_summaries)
        participants = list({m.get("username", "?") for m in old_messages if m.get("username")})

        # 提取最后 20 条中的关键决策作为 key_points
        key_points = []
        for m in old_messages[-20:]:
            msg = (m.get("message") or "").strip()
            if len(msg) > 15 and not msg.startswith(("http", "@")):
                key_points.append(f"[{m.get('username', '?')}] {msg[:120]}")
        key_points = key_points[-10:]

        topic = f"容量清理-{time.strftime('%Y-%m-%d_%H%M')} ({len(old_messages)}条)"

        # 持久化
        self.memory.save_conversation_segment(
            channel_id=channel_id,
            topic=topic,
            summary=combined_summary,
            participants=participants,
            key_points=key_points,
        )

        # 线程回复 (挂载在缓存中最新一条消息下)
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

    # ============================================================
    # 内部: 调 LLM 做摘要
    # ============================================================

    async def _summarize_message_batch(
        self,
        messages: list[dict],
        channel_id: str,
        context_messages: list[dict] | None = None,
    ) -> str:
        """调用 LLM 对一批消息做结构化摘要 (支持注入前序上下文)

        Args:
            messages: 当前要摘要的消息批次
            channel_id: 频道 ID (保留参数, 未来可按频道定制提示)
            context_messages: 前序消息 (LLM 参考, 不会出现在摘要输出里)
        """
        cfg = self.config

        # ── 格式化前序上下文 ──
        context_text = ""
        if context_messages:
            ctx_lines = []
            for m in context_messages[-cfg.memory_context_window :]:
                user = m.get("username", "?")
                msg = (m.get("message") or "").strip()[:200]
                ctx_lines.append(f"  {user}: {msg}")
            context_text = (
                "\n【前序对话背景 (仅供参考, 理解上下文用, 不需要重复摘要)】\n"
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
                    dt = datetime.fromtimestamp(ts if ts < 1e12 else ts / 1000.0)
                    time_str = dt.strftime("%H:%M")
                except (ValueError, OSError):
                    pass
            lines.append(f"[{time_str}] {user}: {msg[:300]}")

        conversation_text = "\n".join(lines)

        prompt = (
            "请对以下团队对话记录做简洁的结构化摘要。\n"
            "要求:\n"
            "1. 用 2-3 句话概括这段时间讨论的核心主题和结论\n"
            "2. 如果有明确的决策或行动项, 单独列出\n"
            "3. 省略闲聊和无关内容\n"
            "4. 直接输出摘要, 不要加前缀说明\n"
            f"{context_text}\n"
            f"\n【当前待摘要对话 ({len(messages)} 条)】\n{conversation_text}"
        )

        try:
            result = await self.llm.chat_with_system(
                system_prompt=(
                    "你是一个对话摘要助手。你的任务是将团队聊天记录压缩为"
                    "精炼的结构化摘要, 保留关键信息和决策, 去除冗余。"
                    "如果提供了前序对话背景, 请用它来理解上下文, 但只对"
                    "当前待摘要的对话部分输出摘要。"
                ),
                user_message=prompt,
                max_tokens=1024,
            )
            return result
        except Exception as e:
            log.error("%s [压缩] LLM 摘要失败: %s", trace.prefix(), e)
            return f"(摘要失败: {e})"

    # ============================================================
    # 内部: 发摘要到频道线程
    # ============================================================

    async def _post_summary_thread(
        self, channel_id: str, root_id: str, title: str, content: str
    ) -> None:
        """将摘要以线程回复形式发送到频道

        线程消息不会出现在主聊天流, 用户需要点开才能看到。
        这样既让团队可见知识沉淀, 又不打扰正常对话。
        """
        # 截断过长的摘要 (Mattermost 单条消息限制 ~4MB, 但实际显示体验上 4000 字符为宜)
        if len(content) > _THREAD_MAX_CHARS:
            content = content[:_THREAD_MAX_CHARS] + "\n\n...(摘要已截断)"

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
