"""Mattermost delivery boundary and transactional outbox collection."""

from __future__ import annotations

import asyncio
import random
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING

from ..client import PROP_FROM_BOT, PROP_TRUE
from ..config import config
from ..control_plane import MattermostAccessGuard, MattermostScopeResolver, OutboundMessage
from ..logger import get_logger

if TYPE_CHECKING:
    from ..execution import ArtifactRepository
    from .context import BotIdentity
    from .stream import MattermostStream
    from .views import ResponseView

log = get_logger(__name__)

OUTBOUND_COLLECTOR: ContextVar[list[OutboundMessage] | None] = ContextVar(
    "mmag_outbound_collector", default=None
)


class MattermostDelivery:
    def __init__(
        self,
        mm_client,
        memory,
        identity: BotIdentity,
        stats: dict[str, int],
        *,
        artifacts: ArtifactRepository | None = None,
        outbox_store=None,
        scope_resolver: MattermostScopeResolver | None = None,
        access_guard: MattermostAccessGuard | None = None,
    ):
        self.mm = mm_client
        self.memory = memory
        self.identity = identity
        self.stats = stats
        self.artifacts = artifacts
        self.outbox_store = outbox_store
        self.scope_resolver = scope_resolver or MattermostScopeResolver(
            mm_client,
            installation_id=config.mm_installation_id,
            tenant_id=config.mm_tenant_id,
        )
        self.access_guard = access_guard or MattermostAccessGuard(
            mm_client,
            installation_id=config.mm_installation_id,
            tenant_id=config.mm_tenant_id,
        )
        from .render import MattermostRenderer

        self.renderer = MattermostRenderer(
            max_chars=config.mm_response_max_chars,
            action_callback_url=config.mm_action_callback_url,
        )

    async def typing_indicator(self, channel_id: str, duration: float | None = None) -> None:
        if duration is None:
            duration = config.typing_delay_min + random.random() * (
                config.typing_delay_max - config.typing_delay_min
            )
        await asyncio.sleep(duration)

    async def send_ack(self, post: dict) -> str | None:
        try:
            return await asyncio.to_thread(
                self.mm.send_post,
                channel_id=post["channel_id"],
                message=config.mm_ack_message,
                root_id=self.thread_root(post),
                props={
                    PROP_FROM_BOT: PROP_TRUE,
                    "mmag_kind": "status",
                    "mmag_status": "running",
                },
            )
        except Exception as error:
            log.debug("状态确认发送失败: %s", error)
            return None

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
        scope_id = self.scope(post)
        actor_id = str(post.get("user_id") or "")
        await self.access_guard.require(
            actor_id,
            scope_id,
            channel_id=str(post.get("channel_id") or ""),
        )
        collector = OUTBOUND_COLLECTOR.get()
        if collector is not None:
            collector.append(
                OutboundMessage(
                    conversation_id=post["channel_id"],
                    channel_id=post["channel_id"],
                    text=message,
                    props={PROP_FROM_BOT: PROP_TRUE},
                    root_id=self.thread_root(post),
                    scope_id=scope_id,
                    actor_id=actor_id,
                )
            )
            return "outbox:pending"
        try:
            post_id = self.mm.send_post(
                channel_id=post["channel_id"],
                message=message,
                root_id=self.thread_root(post),
                props={PROP_FROM_BOT: PROP_TRUE},
            )
        except Exception as error:
            log.error("send_post 异常: %s", error)
            return None
        if not post_id:
            log.error("send_post 返回 None! channel=%s", post["channel_id"][:8])
            return None
        self._record(
            post_id,
            post["channel_id"],
            message,
            self.thread_root(post),
            scope_id,
        )
        return post_id

    async def reply_view(
        self,
        post: dict,
        view: ResponseView,
        *,
        update_post_id: str = "",
        delivery_key: str = "",
    ) -> tuple[str, ...]:
        rendered = self.renderer.render(view)
        root_id = self.thread_root(post)
        scope_id = self.scope(post)
        run_key = delivery_key or view.run_id or str(post.get("id") or "response")
        messages: list[OutboundMessage] = []
        for index, chunk in enumerate(rendered.chunks):
            messages.append(
                OutboundMessage(
                    conversation_id=post["channel_id"],
                    channel_id=post["channel_id"],
                    text=chunk,
                    props=rendered.props if index == 0 else {PROP_FROM_BOT: PROP_TRUE},
                    idempotency_key=f"{run_key}:response:{index}",
                    root_id=root_id,
                    message_kind=view.kind.value,
                    scope_id=scope_id,
                    actions=rendered.actions if index == 0 else (),
                    update_post_id=update_post_id if index == 0 else "",
                    actor_id=str(post.get("user_id") or ""),
                )
            )
        if rendered.artifact_refs:
            messages.append(
                OutboundMessage(
                    conversation_id=post["channel_id"],
                    channel_id=post["channel_id"],
                    text="📎 交付产物",
                    props={PROP_FROM_BOT: PROP_TRUE, "mmag_kind": "artifact"},
                    idempotency_key=f"{run_key}:artifacts",
                    root_id=root_id,
                    message_kind="artifact",
                    scope_id=scope_id,
                    artifact_refs=rendered.artifact_refs,
                    actor_id=str(post.get("user_id") or ""),
                )
            )
        collector = OUTBOUND_COLLECTOR.get()
        if collector is not None:
            collector.extend(messages)
            return tuple("outbox:pending" for _ in messages)
        remote_ids: list[str] = []
        for message in messages:
            remote_ids.append(await self.deliver(message))
        return tuple(remote_ids)

    def stream(
        self,
        post: dict,
        run_id: str,
        *,
        post_id: str = "",
    ) -> MattermostStream:
        from .stream import MattermostStream

        return MattermostStream(
            self.mm,
            post,
            run_id,
            post_id=post_id,
            min_interval_seconds=config.mm_stream_update_interval_ms / 1000,
            min_chars=config.mm_stream_min_chars,
            max_chars=config.mm_response_max_chars,
        )

    async def deliver(self, outbound: OutboundMessage) -> str:
        channel_id = outbound.channel_id or outbound.conversation_id
        await self.access_guard.require(
            outbound.actor_id,
            outbound.scope_id,
            channel_id=channel_id,
        )
        file_ids = list(outbound.file_ids)
        if outbound.artifact_refs:
            if self.artifacts is None:
                raise RuntimeError("Artifact delivery is not configured")
            if not outbound.scope_id:
                raise PermissionError("Artifact delivery has no trusted scope")
            if len(file_ids) > len(outbound.artifact_refs):
                raise RuntimeError("Persisted file IDs exceed Artifact delivery intent")
            for ref in outbound.artifact_refs[len(file_ids) :]:
                stored, path = self.artifacts.resolve(ref, scope_id=outbound.scope_id)
                file_id = await self.mm.upload_file_async(
                    channel_id,
                    stored.filename,
                    path.read_bytes(),
                    stored.media_type,
                )
                if not file_id:
                    raise RuntimeError(f"Artifact upload failed: {stored.filename}")
                file_ids.append(file_id)
                if self.outbox_store is not None and outbound.idempotency_key:
                    self.outbox_store.save_delivery_files(
                        outbound.idempotency_key, tuple(file_ids)
                    )
        if outbound.update_post_id and not file_ids:
            post_id = await self.mm.update_post_async(
                outbound.update_post_id,
                outbound.text,
                props=dict(outbound.props),
            )
        else:
            post_id = await self.mm.send_post_async(
                channel_id=channel_id,
                message=outbound.text,
                root_id=outbound.root_id,
                props=dict(outbound.props),
                file_ids=file_ids,
                pending_post_id=outbound.idempotency_key,
            )
        if not post_id:
            raise RuntimeError("Mattermost delivery failed")
        self._record(
            post_id,
            channel_id,
            outbound.text,
            outbound.root_id,
            outbound.scope_id,
        )
        return post_id

    @staticmethod
    def thread_root(post: dict) -> str:
        return str(post.get("root_id") or post.get("id") or "")

    def scope(self, post: dict) -> str:
        return self.scope_resolver.resolve_post(post).id

    def _record(
        self,
        post_id: str,
        channel_id: str,
        text: str,
        root_id: str = "",
        scope_id: str = "",
    ) -> None:
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
                "root_id": root_id,
                "_scope_id": scope_id,
            }
        ):
            self.stats["dropped_messages"] += 1
