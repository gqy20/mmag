"""Ephemeral Mattermost stream projection for Runtime text deltas."""

from __future__ import annotations

import asyncio
import time

from ..client import PROP_FROM_BOT, PROP_TRUE
from ..logger import get_logger
from ..runtimes import RunEvent, RunEventKind
from .render import MattermostRenderer

log = get_logger(__name__)


class MattermostStream:
    """Throttle text deltas into one editable Mattermost thread post."""

    def __init__(
        self,
        mm_client,
        post: dict,
        run_id: str,
        *,
        post_id: str = "",
        min_interval_seconds: float = 0.75,
        min_chars: int = 80,
        max_chars: int = 12_000,
    ) -> None:
        self.mm = mm_client
        self.channel_id = str(post["channel_id"])
        self.root_id = str(post.get("root_id") or post.get("id") or "")
        self.run_id = run_id
        self.post_id = post_id
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.min_chars = max(1, min_chars)
        self.max_chars = max(256, max_chars)
        self._buffer = ""
        self._round = 0
        self._last_chars = 0
        self._last_update = 0.0
        self._disabled = False
        self._lock = asyncio.Lock()

    async def __call__(self, event: RunEvent) -> None:
        if self._disabled or event.kind is not RunEventKind.TEXT_DELTA or not event.text:
            return
        async with self._lock:
            self._append(event)
            if not self._should_publish():
                return
            try:
                await self._publish()
            except Exception as error:
                self._disabled = True
                log.warning("Mattermost 流式更新已降级为最终回复: %s", error)

    def _append(self, event: RunEvent) -> None:
        if event.round != self._round:
            self._round = event.round
            self._buffer = event.text
            self._last_chars = 0
            return
        self._buffer += event.text

    def _should_publish(self) -> bool:
        if not self._buffer:
            return False
        if not self.post_id:
            return True
        now = time.monotonic()
        elapsed = now - self._last_update
        growth = len(self._buffer) - self._last_chars
        return elapsed >= self.min_interval_seconds and (
            growth >= self.min_chars or elapsed >= self.min_interval_seconds * 3
        )

    async def _publish(self) -> None:
        message = self._message()
        if self.post_id:
            remote_id = await self.mm.update_post_async(
                self.post_id,
                message,
                props=self._props(),
            )
        else:
            remote_id = await self.mm.send_post_async(
                channel_id=self.channel_id,
                message=message,
                root_id=self.root_id,
                props=self._props(),
                pending_post_id=f"{self.run_id}:stream",
            )
        if not remote_id:
            raise RuntimeError("Mattermost stream post failed")
        self.post_id = remote_id
        self._last_chars = len(self._buffer)
        self._last_update = time.monotonic()

    def _message(self) -> str:
        content = MattermostRenderer.clean(self._buffer, self.max_chars)
        return f"### ✨ 正在生成\n\n{content}\n\n_Run：`{self.run_id}`_"

    @staticmethod
    def _props() -> dict[str, str]:
        return {
            PROP_FROM_BOT: PROP_TRUE,
            "mmag_kind": "stream",
            "mmag_status": "running",
        }
