"""
_get_ack + typing indicator 单元测试

覆盖:
  - send_ack: 线程回复可配置确认，带正确 root_id / props
  - _send_get_ack: 不记入 memory / stats
  - _send_get_ack: send_post 异常不外泄
  - _typing_loop: 可被 cancel 正常退出
  - _typing_loop: send_typing 异常不中断循环
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.application import BotIdentity, MattermostDelivery  # noqa: E402
from mmag.client import PROP_FROM_BOT, PROP_TRUE  # noqa: E402


def _make_delivery(bot_user_id: str = "u_bot") -> MattermostDelivery:
    mm = MagicMock()
    memory = MagicMock()
    stats = {"responses": 0, "dropped_messages": 0}
    return MattermostDelivery(mm, memory, BotIdentity(bot_user_id, "agent2"), stats)


# ---- _send_get_ack ----


class TestSendGetAck:
    def test_sends_configured_status_as_thread_reply(self):
        a = _make_delivery()
        a.mm.send_post.return_value = "post_ack_123"
        post = {"channel_id": "ch1", "id": "msg_abc", "message": "@agent2 hi"}

        asyncio.run(a.send_ack(post))

        a.mm.send_post.assert_called_once_with(
            channel_id="ch1",
            message="收到，正在处理。",
            root_id="msg_abc",
            props={
                PROP_FROM_BOT: PROP_TRUE,
                "mmag_kind": "status",
                "mmag_status": "running",
            },
        )

    def test_does_not_log_to_memory(self):
        a = _make_delivery()
        a.mm.send_post.return_value = "post_ack_123"
        post = {"channel_id": "ch1", "id": "msg_abc", "message": "hi"}

        asyncio.run(a.send_ack(post))

        a.memory.log_message.assert_not_called()

    def test_does_not_increment_stats(self):
        a = _make_delivery()
        a.mm.send_post.return_value = "post_ack_123"
        post = {"channel_id": "ch1", "id": "msg_abc", "message": "hi"}

        asyncio.run(a.send_ack(post))

        assert a.stats["responses"] == 0

    def test_send_post_exception_does_not_raise(self):
        a = _make_delivery()
        a.mm.send_post.side_effect = RuntimeError("network down")
        post = {"channel_id": "ch1", "id": "msg_abc", "message": "hi"}

        # Should not raise
        asyncio.run(a.send_ack(post))

    def test_missing_post_id_sends_empty_root(self):
        """post 没有 id 字段时, root_id 为空 (退化为普通消息)"""
        a = _make_delivery()
        a.mm.send_post.return_value = "post_ack"
        post = {"channel_id": "ch1", "message": "hi"}

        asyncio.run(a.send_ack(post))

        call_kwargs = a.mm.send_post.call_args
        assert call_kwargs.kwargs["root_id"] == ""


# ---- _typing_loop ----


class TestTypingLoop:
    def test_cancel_stops_loop_cleanly(self):
        """cancel 后 _typing_loop 正常退出,不抛 CancelledError"""
        a = _make_delivery()
        a.mm.send_typing.return_value = True

        async def run():
            task = asyncio.create_task(a.typing_loop("ch1"))
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run())

    def test_send_typing_exception_does_not_crash(self):
        """单次 send_typing 失败不应中断循环"""
        a = _make_delivery()
        a.mm.send_typing.side_effect = RuntimeError("timeout")

        async def run():
            task = asyncio.create_task(a.typing_loop("ch1"))
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # Should not raise
        asyncio.run(run())

    def test_sends_typing_repeatedly(self):
        """循环期间多次调用 send_typing — 用 side_effect 在第 2 次调用后 cancel"""
        a = _make_delivery()
        calls = []
        task_holder: list = []

        def track_and_cancel(channel_id):
            calls.append(channel_id)
            if len(calls) >= 2 and task_holder:
                task_holder[0].cancel()

        a.mm.send_typing.side_effect = track_and_cancel

        async def run():
            # Patch sleep to be instant so the loop spins fast
            import mmag.application.delivery as delivery_mod

            original = delivery_mod.asyncio.sleep

            async def instant_sleep(seconds):
                await original(0)

            delivery_mod.asyncio.sleep = instant_sleep
            try:
                task = asyncio.create_task(a.typing_loop("ch1"))
                task_holder.append(task)
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                delivery_mod.asyncio.sleep = original

        asyncio.run(run())
        assert len(calls) >= 2
