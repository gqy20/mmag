"""Signed one-time Mattermost actions and a small local callback adapter."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from ..control_plane import SQLiteControlPlane

_ALLOWED_ACTIONS = frozenset({"approve", "reject", "retry", "download", "rework"})
_MAX_TOKEN_BYTES = 8_192
_MAX_REQUEST_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class ActionClaims:
    jti: str
    action: str
    target: str
    scope_id: str
    run_id: str
    conversation_id: str
    root_id: str
    requested_by: str
    expires_at: float


class ActionTokenError(ValueError):
    pass


class ActionTokenService:
    def __init__(
        self,
        secret: str,
        store: SQLiteControlPlane,
        *,
        ttl_seconds: int = 600,
    ) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("MM_ACTION_SIGNING_SECRET must contain at least 32 bytes")
        if ttl_seconds < 30 or ttl_seconds > 600:
            raise ValueError("Action token TTL must be between 30 and 600 seconds")
        self._secret = secret.encode()
        self.store = store
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        action: str,
        target: str,
        scope_id: str,
        run_id: str,
        conversation_id: str,
        root_id: str,
        requested_by: str,
    ) -> str:
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(f"unsupported action {action!r}")
        now = time.time()
        jti = uuid.uuid4().hex
        expires_at = now + self.ttl_seconds
        claims: dict[str, str | int | float] = {
            "v": 1,
            "jti": jti,
            "act": action,
            "sub": target,
            "scp": scope_id,
            "run": run_id,
            "con": conversation_id,
            "root": root_id,
            "req": requested_by,
            "exp": expires_at,
        }
        encoded = self._encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signature = self._encode(hmac.digest(self._secret, encoded.encode(), "sha256"))
        self.store.create_action_token(
            jti=jti,
            action=action,
            target=target,
            scope_id=scope_id,
            run_id=run_id,
            expires_at=expires_at,
        )
        return f"{encoded}.{signature}"

    def consume(self, token: str, *, actor_id: str) -> ActionClaims:
        if not actor_id:
            raise ActionTokenError("Action actor is required")
        claims = self.verify(token)
        now = time.time()
        if not self.store.consume_action_token(claims.jti, actor_id=actor_id, now=now):
            raise ActionTokenError("Action token was already used or expired")
        return claims

    def verify(self, token: str) -> ActionClaims:
        if len(token.encode()) > _MAX_TOKEN_BYTES:
            raise ActionTokenError("Action token is too large")
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected = self._encode(hmac.digest(self._secret, encoded.encode(), "sha256"))
            if not hmac.compare_digest(supplied_signature, expected):
                raise ActionTokenError("Invalid action signature")
            raw = json.loads(self._decode(encoded))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, ActionTokenError):
                raise
            raise ActionTokenError("Malformed action token") from error
        if not isinstance(raw, dict) or raw.get("v") != 1:
            raise ActionTokenError("Unsupported action token")
        claims = self._claims(raw)
        now = time.time()
        if claims.expires_at < now:
            raise ActionTokenError("Action token has expired")
        return claims

    @staticmethod
    def _claims(raw: dict[str, Any]) -> ActionClaims:
        try:
            claims = ActionClaims(
                jti=str(raw["jti"]),
                action=str(raw["act"]),
                target=str(raw["sub"]),
                scope_id=str(raw["scp"]),
                run_id=str(raw["run"]),
                conversation_id=str(raw["con"]),
                root_id=str(raw["root"]),
                requested_by=str(raw["req"]),
                expires_at=float(raw["exp"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ActionTokenError("Incomplete action token") from error
        if claims.action not in _ALLOWED_ACTIONS:
            raise ActionTokenError("Unsupported action")
        if not all(
            (
                claims.jti,
                claims.target,
                claims.scope_id,
                claims.conversation_id,
                claims.requested_by,
            )
        ):
            raise ActionTokenError("Incomplete action token")
        return claims

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> str:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode()


class ActionCallbackServer:
    """Local HTTP adapter intended to sit behind the configured HTTPS proxy."""

    def __init__(
        self,
        host: str,
        port: int,
        callback: Callable[
            [dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]
        ],
        *,
        path: str = "/actions",
    ) -> None:
        self.host = host
        self.port = port
        self.path = path or "/actions"
        self.callback = callback
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        callback = self.callback
        expected_path = self.path

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != expected_path:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if length < 1 or length > _MAX_REQUEST_BYTES:
                    self.send_error(413)
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("payload is not an object")
                    future: Future[dict[str, Any]] = asyncio.run_coroutine_threadsafe(
                        callback(payload), loop
                    )
                    result = future.result(timeout=20)
                    body = json.dumps(result, ensure_ascii=False).encode()
                    self.send_response(200)
                except Exception:
                    body = json.dumps(
                        {"ephemeral_text": "操作未完成，请使用文本命令重试。"},
                        ensure_ascii=False,
                    ).encode()
                    self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mmag-actions",
            daemon=True,
        )
        self._thread.start()

    async def close(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is None:
            return
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
