"""Mattermost delivery boundary and transactional outbox collection."""

from __future__ import annotations

import asyncio
import random
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING

from ..client import PROP_FROM_BOT, PROP_TRUE
from ..config import config
from ..control_plane import OutboundMessage
from ..logger import get_logger

if TYPE_CHECKING:
    from .context import BotIdentity

log = get_logger(__name__)

OUTBOUND_COLLECTOR: ContextVar[list[OutboundMessage] | None] = ContextVar(
    "mmag_outbound_collector", default=None
)


class MattermostDelivery:
    def __init__(self, mm_client, memory, identity: BotIdentity, stats: dict[str, int]):
        self.mm = mm_client
        self.memory = memory
        self.identity = identity
        self.stats = stats

    async def typing_indicator(self, channel_id: str, duration: float | None = None) -> None:
        if duration is None:
            duration = config.typing_delay_min + random.random() * (
                config.typing_delay_max - config.typing_delay_min
            )
        await asyncio.sleep(duration)

    async def send_ack(self, post: dict) -> None:
        try:
            await asyncio.to_thread(
                self.mm.send_post,
                channel_id=post["channel_id"],
                message="get",
                root_id=post.get("id", ""),
                props={PROP_FROM_BOT: PROP_TRUE},
            )
        except Exception as error:
            log.debug("get ack 发送失败: %s", error)

    async def typing_loop(self, channel_id: str) -> None:
        try:
            while True:
                await asyncio.to_thread(self.mm.send_typing, channel_id)
                await asyncio.sleep(2.5)
        except asyncio.CancelledError:
            pass
        except Exception as error:
            log.debug("typing loop 异常: %s", error)

    async def reply(self, post: dict, message: str) -> str | None:
        if not message:
            log.warning("reply(): 消息为空，跳过发送")
            return None
        collector = OUTBOUND_COLLECTOR.get()
        if collector is not None:
            collector.append(
                OutboundMessage(
                    conversation_id=post["channel_id"],
                    channel_id=post["channel_id"],
                    text=message,
                    props={PROP_FROM_BOT: PROP_TRUE},
                )
            )
            return "outbox:pending"
        try:
            post_id = self.mm.send_post(
                channel_id=post["channel_id"],
                message=message,
                props={PROP_FROM_BOT: PROP_TRUE},
            )
        except Exception as error:
            log.error("send_post 异常: %s", error)
            return None
        if not post_id:
            log.error("send_post 返回 None! channel=%s", post["channel_id"][:8])
            return None
        self._record(post_id, post["channel_id"], message)
        return post_id

    async def deliver(self, outbound: OutboundMessage) -> str:
        channel_id = outbound.channel_id or outbound.conversation_id
        post_id = await self.mm.send_post_async(
            channel_id=channel_id,
            message=outbound.text,
            props=dict(outbound.props),
            pending_post_id=outbound.idempotency_key,
        )
        if not post_id:
            raise RuntimeError("Mattermost delivery failed")
        self._record(post_id, channel_id, outbound.text)
        return post_id

    def _record(self, post_id: str, channel_id: str, text: str) -> None:
        self.stats["responses"] += 1
        if not self.memory.log_message(
            {
                "id": post_id,
                "channel_id": channel_id,
                "user_id": self.identity.user_id,
                "username": self.identity.username,
                "message": text,
                "create_at": int(time.time() * 1000),
                "type": "",
                "root_id": "",
            }
        ):
            self.stats["dropped_messages"] += 1
