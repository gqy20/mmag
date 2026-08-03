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
from urllib.parse import parse_qs, urlsplit

from ..logger import get_logger, log_event

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from ..control_plane import SQLiteControlPlane

_ALLOWED_ACTIONS = frozenset(
    {
        "approve", "reject", "retry", "download", "rework",
        "pskill_run", "pskill_edit", "pskill_activate", "pskill_archive",
        "pskill_versions",
        "case_save", "case_good", "case_bad", "case_draft",
        "memory_forget",
        "persona_add", "persona_publish", "persona_archive",
        "persona_reply_approve", "persona_reply_edit", "persona_reply_submit",
        "persona_reply_reject",
        "persona_policy_edit", "persona_policy_submit",
    }
)
_MAX_TOKEN_BYTES = 8_192
_MAX_REQUEST_BYTES = 65_536
log = get_logger(__name__)

_SLASH_COMMAND_HELP = """### MMAG 子命令

- `/mmag help`：显示本帮助
- `/mmag ask <goal>`：让默认 Agent 处理目标
- `/mmag agents`：列出 Agent
- `/mmag skills [agent]`：列出 Skill
- `/mmag run <agent> <goal>`：指定 Agent 运行
- `/mmag status [run-id]`：查看运行状态

当前已开放命令发现；业务子命令接入现有权限、Inbox/Outbox 和审批链后再逐项开放。"""


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


class SlashCommandAdapter:
    """Authenticate Mattermost slash requests and return private command discovery."""

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise ValueError("MM_SLASH_COMMAND_TOKEN must not be empty")
        self._token = token

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        supplied_token = str(payload.get("token") or "")
        if len(supplied_token.encode()) > _MAX_TOKEN_BYTES or not hmac.compare_digest(
            supplied_token, self._token
        ):
            raise PermissionError("invalid Mattermost slash command token")
        if str(payload.get("command") or "") != "/mmag":
            raise ValueError("unexpected Mattermost slash command")
        if not str(payload.get("user_id") or "") or not str(
            payload.get("channel_id") or ""
        ):
            raise ValueError("Mattermost slash command actor and channel are required")

        text = str(payload.get("text") or "").strip()
        subcommand = text.split(maxsplit=1)[0].lower() if text else "help"
        prefix = "" if subcommand == "help" else f"`{subcommand}` 子命令尚未开放。\n\n"
        log_event(
            log,
            "mattermost.slash_command",
            status="completed",
            help_requested=subcommand == "help",
        )
        return {
            "response_type": "ephemeral",
            "text": f"{prefix}{_SLASH_COMMAND_HELP}",
        }


def _parse_form_payload(raw: bytes) -> dict[str, Any]:
    try:
        values = parse_qs(
            raw.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("malformed form payload") from error
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("duplicate form field")
    return {key: items[0] for key, items in values.items()}


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
    """Local action/slash callback gateway for a configured HTTPS proxy."""

    def __init__(
        self,
        host: str,
        port: int,
        callback: Callable[
            [dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]
        ]
        | None = None,
        *,
        path: str = "/actions",
        command_callback: Callable[
            [dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]
        ]
        | None = None,
        command_path: str = "/integrations/commands",
    ) -> None:
        if callback is None and command_callback is None:
            raise ValueError("at least one callback route is required")
        self.host = host
        self.port = port
        self.path = path or "/actions"
        self.callback = callback
        self.command_path = command_path
        self.command_callback = command_callback
        if callback is not None and command_callback is not None and self.path == command_path:
            raise ValueError("action and slash command callback paths must differ")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        routes = {
            route_path: (route_callback, is_form)
            for route_path, route_callback, is_form in (
                (self.path, self.callback, False),
                (self.command_path, self.command_callback, True),
            )
            if route_callback is not None
        }

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                route = routes.get(urlsplit(self.path).path)
                if route is None:
                    self.send_error(404)
                    return
                route_callback, is_form = route
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if length < 1 or length > _MAX_REQUEST_BYTES:
                    self.send_error(413)
                    return
                try:
                    raw = self.rfile.read(length)
                    if is_form:
                        content_type = self.headers.get("Content-Type", "")
                        if not content_type.startswith("application/x-www-form-urlencoded"):
                            raise ValueError("slash command requires a form payload")
                        payload = _parse_form_payload(raw)
                    else:
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise ValueError("payload is not an object")
                    future: Future[dict[str, Any]] = asyncio.run_coroutine_threadsafe(
                        route_callback(payload), loop
                    )
                    result = future.result(timeout=20)
                    body = json.dumps(result, ensure_ascii=False).encode()
                    self.send_response(200)
                except Exception as error:
                    log_event(
                        log,
                        "mattermost.callback",
                        level=40,
                        status="failed",
                        error_code=type(error).__name__,
                    )
                    response = (
                        {
                            "response_type": "ephemeral",
                            "text": "命令未完成，请检查配置后重试。",
                        }
                        if is_form
                        else {"ephemeral_text": "操作未完成，请使用文本命令重试。"}
                    )
                    body = json.dumps(response, ensure_ascii=False).encode()
                    self.send_response(403 if isinstance(error, PermissionError) else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mmag-callbacks",
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
