"""
Mattermost WebSocket 客户端 — 官方协议实现

封装内容 (按 Mattermost webapp 官方做法):
  - URL 构造 (含断线续传参数)
  - WebSocket 连接 (Bearer 认证头)
  - Hello 握手 + authentication_challenge 认证
  - 心跳 (每 30s 主动 ping)
  - 序列号追踪 (发现不连续仅记录, Bot 场景容忍丢包)
  - 自动重连 + 指数退避

调用方只需:
  ws = WebSocketClient(url, token, on_event=my_handler)
  await ws.run()            # 阻塞运行直到 ws.close()
  await ws.send("ping")     # 主动发消息 (可选)
  await ws.close()          # 主动停止

设计动机 (从 agent.py 拆出):
  - 复杂的状态机 / 重连逻辑不该和"消息处理"耦合
  - Agent 类应聚焦业务: 怎么响应消息、用哪些工具、记什么知识
  - 单元测试可以 mock WebSocket 协议单独验证 Agent 逻辑
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import TYPE_CHECKING, Any

import websockets

from .logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger(__name__)


# ---- 重连参数 (参考 webapp/platform/client/src/websocket.ts) ----
_MIN_RETRY_S = 3
_MAX_RETRY_S = 300  # 5 分钟
_JITTER_RANGE_S = 2
_MAX_FAILS_BEFORE_BACKOFF = 7
_PING_INTERVAL_S = 30
_OPEN_TIMEOUT_S = 10


class WebSocketClient:
    """Mattermost WebSocket 客户端

    Args:
        url: WebSocket 完整 URL (含 scheme://host/path)
        token: Mattermost Bearer Token
        on_event: 服务端事件回调 (async) — posted / typing / status_change 等
        on_response: 客户端请求响应回调 (sync 可) — 认证结果 / ping 回复
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        on_response: Callable[[dict], None] | None = None,
    ) -> None:
        self.url = url
        self.token = token
        self._on_event = on_event
        self._on_response = on_response

        # 连接状态 (协议要求)
        self._conn_id: str = ""
        self._server_seq: int = 0
        self._client_seq: int = 1
        self._connect_fail_count: int = 0
        self._last_err_code: str | None = None
        self._ping_task: asyncio.Task | None = None
        self._ws: Any = None  # 当前连接 (websockets.WebSocketClientProtocol)

        # 生命周期
        self._running = False

    # ============================================================
    # 公共 API
    # ============================================================

    async def run(self) -> None:
        """主循环: 持续重连 + 处理事件, 直到 close() 被调用"""
        self._running = True
        while self._running:
            try:
                await self._session()
            except Exception as e:
                log.error(f"       ❌ 连接异常: {e}", exc_info=True)
                self._connect_fail_count += 1
            finally:
                await self._cleanup_ping_task()

            if not self._running:
                break

            # 指数退避
            retry_s = _MIN_RETRY_S
            if self._connect_fail_count > _MAX_FAILS_BEFORE_BACKOFF:
                retry_s = min(
                    _MIN_RETRY_S * self._connect_fail_count**2, _MAX_RETRY_S
                )
            retry_s += random.random() * _JITTER_RANGE_S  # jitter
            log.info(
                f"       ⏳ {retry_s:.1f}s 后重连 (第{self._connect_fail_count}次)..."
            )
            await asyncio.sleep(retry_s)

    async def send(self, action: str, data: dict | None = None) -> None:
        """发送任意 action 到服务端 (如 ping / authentication_challenge)

        Raises:
            RuntimeError: 未连接时调用
        """
        if self._ws is None:
            raise RuntimeError("WebSocket 未连接, 无法 send")
        seq = self._client_seq
        self._client_seq += 1
        msg = {"action": action, "seq": seq}
        if data is not None:
            msg["data"] = data
        await self._ws.send(json.dumps(msg))

    async def close(self) -> None:
        """主动停止主循环 (会关闭当前连接)"""
        self._running = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ============================================================
    # 内部: 一次完整连接会话
    # ============================================================

    async def _session(self) -> None:
        """一次完整会话: 连接 → 握手 → 认证 → 心跳 → 事件循环"""
        ws_url = self._build_url()
        log.info(
            f"       → {ws_url[:80]}{'...' if len(ws_url) > 80 else ''}"
        )

        async with websockets.connect(
            ws_url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            open_timeout=_OPEN_TIMEOUT_S,
            ping_interval=None,  # 我们自己管理心跳
            ping_timeout=None,
        ) as ws:
            self._ws = ws
            log.info("       ✅ WebSocket 已连接")

            # Step 1: 收 Hello (服务器首条推送)
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            if hello.get("event") != "hello":
                log.warning(f"       ⚠️ 首条非 hello: {hello.get('event', '?')}")

            new_conn_id = hello.get("data", {}).get("connection_id", "")
            server_ver = hello.get("data", {}).get("server_version", "?")

            # 如果 conn_id 变了说明是长时间断线或服务端重启
            if self._conn_id and self._conn_id != new_conn_id:
                log.warning("       ⚠️ connection_id 变化, 可能有遗漏消息")

            self._conn_id = new_conn_id
            # 官方做法: hello 的 seq 之后紧接着就是下一个事件
            self._server_seq = hello.get("seq", 0) + 1
            self._client_seq = 1
            self._connect_fail_count = 0
            self._last_err_code = None
            log.info(f"       📨 Hello | id={self._conn_id[:12]}... v{server_ver}")

            # Step 2: 发送认证 (官方: onopen 时立即发)
            await self.send("authentication_challenge", {"token": self.token})
            log.info("       🔑 认证请求已发送")

            # Step 3: 启动心跳
            self._ping_task = asyncio.create_task(self._ping_loop(ws), name="ws-ping")

            # Step 4: 事件循环
            try:
                async for raw_msg in ws:
                    await self._dispatch(raw_msg)
            except websockets.ConnectionClosed as e:
                log.warning(f"       🔌 WebSocket 断开: code={e.code}")
                self._last_err_code = str(e.code)

    async def _dispatch(self, raw: str) -> None:
        """解析并分发单条 WebSocket 消息

        官方协议区分两种消息类型 (见 websocket_client.go Listen()):
          1. **Event** (服务端推送): 有 `event` 字段, 带递增 `seq`
          2. **Response** (请求回复): 有 `seq_reply` 字段, 有 `status`
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.debug(f"       ⚠️ 无法解析 JSON: {raw[:80]}")
            return

        # ── Response (同步处理即可) ──
        if "seq_reply" in msg:
            if self._on_response is not None:
                self._on_response(msg)
            return

        # ── 序列号校验 (官方: 发现不连续则重连, Bot 场景容忍丢包) ──
        msg_seq = msg.get("seq", -1)
        if msg_seq != self._server_seq:
            log.warning(
                f"       ⚠️ 序列号不连续! 期望={self._server_seq} "
                f"实际={msg_seq} (可能丢失 {msg_seq - self._server_seq} 条事件)"
            )
        self._server_seq = msg_seq + 1

        # ── Event (异步处理, 因为 handler 可能要调 LLM) ──
        if self._on_event is not None:
            await self._on_event(msg)

    async def _ping_loop(self, ws) -> None:
        """心跳循环 (每 30s)"""
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_S)
                await self.send("ping")
                log.debug("       💓 ping sent")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _cleanup_ping_task(self) -> None:
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
        self._ping_task = None
        self._ws = None

    def _build_url(self) -> str:
        """构造含断线续传参数的 WebSocket URL"""
        params = []
        if self._conn_id:
            params.append(f"connection_id={self._conn_id}")
            params.append(f"sequence_number={self._server_seq}")
        if self._last_err_code:
            params.append(f"disconnect_err_code={self._last_err_code}")
        if params:
            return f"{self.url}?{'&'.join(params)}"
        return self.url
