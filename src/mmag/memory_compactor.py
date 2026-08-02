"""
记忆压缩器 — 长期记忆层 (Layer 1→Layer 2) 管理

负责:
  - 定期摘要 (每 N 条消息触发一次, 原始消息永久保留在 message_log)
  - 摘要结果以**线程回复**形式发到频道 (不污染主流)

注意: message_log 是永久存储,不再做"弹出旧消息"这类容量清理。
      摘要功能是把对话凝练成 conversation_segments,供 LLM 长期参考。

设计动机 (从 agent.py 拆出):
  - 摘要是异步后台任务, 不该阻塞消息处理
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

from .client import PROP_FROM_BOT, PROP_SUMMARY, PROP_TRUE
from .logger import get_logger, log_context, log_event
from .runtimes import AgentRuntime, RunContext, RunRequest

if TYPE_CHECKING:
    from .client import MMClient
    from .config import Config
    from .memory import Memory

log = get_logger(__name__)


# 摘要线程展示层字符上限 (超过则截断, 避免 Mattermost 单条消息过大)
_THREAD_MAX_CHARS = 4000


class MemoryCompactor:
    """长期记忆压缩器 (Layer 1→Layer 2)

    Args:
        memory: Memory 实例 (负责 SQLite 读写)
        runtime: AgentRuntime 实例 (负责摘要生成)
        mm_client: MMClient 实例 (负责发摘要线程回复)
        config: 全局配置
    """

    def __init__(
        self,
        memory: Memory,
        runtime: AgentRuntime,
        mm_client: MMClient,
        config: Config,
    ) -> None:
        self.memory = memory
        self.runtime = runtime
        self.mm = mm_client
        self.config = config
        # 每频道消息计数器 — 累计到 summary_interval 触发摘要后归零
        # 比 COUNT(*) 整个 message_log 表快得多,且与"消息是否真的存储"无关
        self._msg_counter: dict[str, int] = {}

    # ============================================================
    # 公共入口
    # ============================================================

    async def maybe_compact(self, channel_id: str) -> None:
        """每条消息后调用 — 内部按阈值判断是否触发摘要

        触发条件:
          - 本频道累计消息数 % summary_interval == 0  →  定期摘要
        """
        self._msg_counter[channel_id] = self._msg_counter.get(channel_id, 0) + 1
        count = self._msg_counter[channel_id]

        if count > 0 and count % self.config.memory_summary_interval == 0:
            await self._periodic_summary(channel_id, count)

    # ============================================================
    # 动作 A: 定期摘要
    # ============================================================

    async def _periodic_summary(self, channel_id: str, count: int) -> None:
        """每 ~summary_interval 条消息触发一次 (不删原消息,原消息永久保留)"""
        summary_interval = self.config.memory_summary_interval
        # 摘要批次大小 = context_window (一次 LLM 处理的合理上限)
        summary_batch = self.config.memory_context_window

        log_event(
            log,
            "memory.compaction_started",
            status="running",
            conversation_id=channel_id,
            message_count=count,
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
            if summary is not None:
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
        log_event(
            log,
            "memory.compaction_completed",
            status="succeeded",
            batch_count=len(all_summaries),
            duration_ms=round(elapsed * 1000),
        )

    # ============================================================
    # 内部: 调 LLM 做摘要
    # ============================================================

    async def _summarize_message_batch(
        self,
        messages: list[dict],
        channel_id: str,
        context_messages: list[dict] | None = None,
    ) -> str | None:
        """调用 LLM 对一批消息做结构化摘要 (支持注入前序上下文)

        Args:
            messages: 当前要摘要的消息批次
            channel_id: 频道 ID (保留参数, 未来可按频道定制提示)
            context_messages: 前序消息 (LLM 参考, 不会出现在摘要输出里)

        Returns:
            摘要文本;LLM 失败时返回 None (调用方应跳过,绝不能把错误信息当摘要持久化)
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
            result = await self.runtime.run(
                RunRequest(
                    context=RunContext(
                        trace_id=log_context.get("trace_id", log_context.new_trace_id()),
                        actor_id="mmag:memory-compactor",
                        conversation_id=channel_id,
                        scope=f"mattermost:channel/{channel_id}",
                    ),
                    messages=({"role": "user", "content": prompt},),
                    system_prompt=(
                        "你是一个对话摘要助手。你的任务是将团队聊天记录压缩为"
                        "精炼的结构化摘要, 保留关键信息和决策, 去除冗余。"
                        "如果提供了前序对话背景, 请用它来理解上下文, 但只对"
                        "当前待摘要的对话部分输出摘要。"
                    ),
                    max_rounds=1,
                    max_tokens=1024,
                    metadata={
                        "route": "default",
                        "model_class": "low-reasoning",
                        "model_policy_ref": "reasoning-low@1.0.0",
                    },
                )
            )
            return result.text
        except Exception as e:
            # 失败时返回 None — 调用方 (._periodic_summary) 会跳过此批次,
            # 避免把错误字符串当合法摘要写入 conversation_segments 污染长期记忆
            log_event(
                log,
                "memory.compaction_failed",
                level=40,
                status="failed",
                conversation_id=channel_id,
                batch_size=len(messages),
                error_code=type(e).__name__,
            )
            return None

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
                props={PROP_FROM_BOT: PROP_TRUE, PROP_SUMMARY: PROP_TRUE},
            )
            if post_id:
                log_event(
                    log,
                    "memory.compaction_delivered",
                    status="succeeded",
                    delivery_id=post_id,
                )
            else:
                log_event(log, "memory.compaction_delivery_failed", level=30, status="failed")
        except Exception as e:
            log_event(
                log,
                "memory.compaction_delivery_failed",
                level=40,
                status="failed",
                error_code=type(e).__name__,
            )
