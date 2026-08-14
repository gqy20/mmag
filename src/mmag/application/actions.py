"""Signed one-time Mattermost actions and a small local callback adapter."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import math
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from ..logger import get_logger, log_event

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from ..agent_packages import AgentPackageRegistry
    from ..control_plane import SQLiteControlPlane
    from ..control_plane.context import MattermostAccessGuard, MattermostScopeResolver
    from ..control_plane.pipeline import MessagePipeline
    from ..skill_packages import SkillPackageRegistry

_ALLOWED_ACTIONS = frozenset(
    {
        "approve",
        "reject",
        "retry",
        "download",
        "rework",
        "pskill_run",
        "pskill_edit",
        "pskill_activate",
        "pskill_archive",
        "pskill_versions",
        "case_save",
        "case_good",
        "case_bad",
        "case_draft",
        "memory_forget",
        "persona_add",
        "persona_publish",
        "persona_archive",
        "persona_reply_approve",
        "persona_reply_edit",
        "persona_reply_submit",
        "persona_reply_reject",
        "persona_policy_edit",
        "persona_policy_submit",
        "task_draft_commit",
        "task_draft_reject",
    }
)
_MAX_TOKEN_BYTES = 8_192
_MAX_REQUEST_BYTES = 65_536
log = get_logger(__name__)

_SLASH_COMMAND_HELP = """### MMAG 子命令

- `/mmag help`：显示本帮助（可用）
- `/mmag agents`：列出已激活 Agent（可用）
- `/mmag skills [agent]`：列出已激活或 Agent 可用的 Skill（可用）
- `/mmag status [run-id]`：查看本人在当前频道的运行状态（可用）
- `/mmag summary today [--tasks]`：总结今天的频道讨论（可用）
- `/mmag summary --since HH:MM [--tasks]`：总结指定时间后的讨论（可用）
- `/mmag summary thread --root <post-id> [--tasks]`：总结指定 Thread（可用）
- `/mmag ask <goal>`：让默认 Agent 处理目标（待开放）
- `/mmag run <agent> <goal>`：指定 Agent 运行（待开放）

`ask/run` 接入现有权限、Inbox/Outbox 和审批链后再开放。"""


@dataclass(frozen=True, slots=True)
class ActionClaims:
    issuer: str
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


class SlashCommandService:
    """Authorized commands backed by Registry state and the durable execution pipeline."""

    def __init__(
        self,
        agent_registry: AgentPackageRegistry,
        skill_registry: SkillPackageRegistry,
        store: SQLiteControlPlane,
        scope_resolver: MattermostScopeResolver,
        access_guard: MattermostAccessGuard,
    ) -> None:
        self.agent_registry = agent_registry
        self.skill_registry = skill_registry
        self.store = store
        self.scope_resolver = scope_resolver
        self.access_guard = access_guard
        self.pipeline: MessagePipeline | None = None

    def attach_pipeline(self, pipeline: MessagePipeline) -> None:
        self.pipeline = pipeline

    async def handle(self, payload: dict[str, Any]) -> str:
        text = str(payload.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        subcommand = parts[0].lower() if parts else "help"
        argument = parts[1].strip() if len(parts) == 2 else ""
        if subcommand == "help":
            return _SLASH_COMMAND_HELP

        actor_id = str(payload.get("user_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        scope = self.scope_resolver.resolve_post({"user_id": actor_id, "channel_id": channel_id})
        await self.access_guard.require(actor_id, scope.id, channel_id=channel_id)

        if subcommand == "agents":
            return self._agents(argument)
        if subcommand == "skills":
            return self._skills(argument)
        if subcommand == "status":
            return self._status(argument, actor_id=actor_id, channel_id=channel_id)
        if subcommand == "summary":
            return await self._summary(argument, payload, actor_id=actor_id, channel_id=channel_id)
        return f"`{subcommand}` 子命令尚未开放。\n\n{_SLASH_COMMAND_HELP}"

    async def _summary(
        self,
        argument: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
        channel_id: str,
    ) -> str:
        if self.pipeline is None:
            return "总结服务尚未就绪，请稍后重试。"
        parsed = self._parse_summary(argument, payload, channel_id=channel_id)
        if isinstance(parsed, str):
            return parsed
        if parsed.get("root_post_id"):
            client = getattr(self.scope_resolver, "client", None)
            if client is None or not hasattr(client, "get_post_async"):
                raise PermissionError("Thread ownership cannot be verified")
            root_post = await client.get_post_async(str(parsed["root_post_id"]))
            if str(root_post.get("channel_id") or "") != channel_id:
                raise PermissionError("Thread belongs to another channel")
            parsed["root_post_id"] = str(
                root_post.get("root_id") or root_post.get("id") or ""
            )
        trigger_id = str(payload.get("trigger_id") or "")
        event_id = f"slash:{trigger_id or uuid.uuid4().hex}"
        root_id = str(parsed.get("root_post_id") or "")
        prompt = (
            "总结这个线程，提取结论和待办"
            if parsed["range"] == "thread"
            else "总结今天的讨论" if parsed.get("summary_period") == "today" else "总结最近的讨论"
        )
        if parsed["tasks_only"]:
            prompt += "，只保留行动项"
        post = {
            "id": event_id,
            "channel_id": channel_id,
            "user_id": actor_id,
            "message": prompt,
            "root_id": root_id,
            "create_at": int(time.time() * 1000),
            "_mmag_entry": "slash",
            "_mmag_summary": parsed,
        }
        from ..control_plane import InboundEvent

        accepted = await self.pipeline.accept(
            InboundEvent(
                event_id=event_id,
                platform="mattermost",
                event_type="slash.summary",
                conversation_id=channel_id,
                actor_id=actor_id,
                occurred_at=time.time(),
                payload={"event": "posted", "data": {"post": post}},
            )
        )
        if not accepted:
            return "该总结命令已经进入队列，无需重复提交。"
        destination = "指定 Thread" if root_id else "当前频道"
        return f"总结任务已排队，完成后会发送到{destination}。"

    @staticmethod
    def _parse_summary(
        argument: str,
        payload: dict[str, Any],
        *,
        channel_id: str,
    ) -> dict[str, Any] | str:
        usage = (
            "用法：`/mmag summary today [--tasks]`、"
            "`/mmag summary --since HH:MM [--tasks]`，或 "
            "`/mmag summary thread --root <post-id> [--tasks]`"
        )
        try:
            tokens = shlex.split(argument)
        except ValueError:
            return usage
        tasks_only = "--tasks" in tokens
        tokens = [token for token in tokens if token != "--tasks"]
        now = datetime.now().astimezone()
        root_id = str(payload.get("root_id") or "")
        summary_period = ""
        since_time = ""
        if tokens == ["today"]:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            summary_period = "today"
            range_name = "recent"
        elif len(tokens) == 2 and tokens[0] == "--since":
            try:
                hour, minute = (int(value) for value in tokens[1].split(":"))
                start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except (TypeError, ValueError):
                return "`--since` 必须是今天的 `HH:MM`。\n\n" + usage
            if start > now:
                return "`--since` 不能晚于当前时间。"
            range_name = "recent"
        elif tokens and tokens[0] == "thread":
            if len(tokens) == 3 and tokens[1] == "--root":
                root_id = tokens[2]
            elif len(tokens) != 1:
                return usage
            if not root_id:
                return (
                    "Mattermost Slash Command 不提供当前 Thread ID，请使用 "
                    "`/mmag summary thread --root <post-id>`。"
                )
            if len(root_id) > 64 or not root_id.isalnum():
                return "Thread Post ID 格式无效。"
            start = now
            range_name = "thread"
        else:
            return usage
        hours = 2 if range_name == "thread" else min(
            24, max(1, math.ceil((now - start).total_seconds() / 3600))
        )
        if range_name == "recent":
            since_time = start.isoformat(timespec="minutes")
        return {
            "channel_id": channel_id,
            "range": range_name,
            "root_post_id": root_id,
            "anchor_post_id": "",
            "hours": hours,
            "limit": 100,
            "since_time": since_time,
            "tasks_only": tasks_only,
            "summary_period": summary_period,
        }

    def _agents(self, argument: str) -> str:
        if argument:
            return "用法：`/mmag agents`"
        lines = ["### 已激活 Agents"]
        for package in self.agent_registry.list():
            metadata = package.manifest.metadata
            default = " · 默认" if package.manifest.routing.default else ""
            lines.append(
                f"- `{metadata.name}@{metadata.version}`{default} — {metadata.description}"
            )
        if len(lines) == 1:
            lines.append("当前没有已激活的 Agent。")
        return "\n".join(lines)

    def _skills(self, agent_name: str) -> str:
        if agent_name and len(agent_name.split()) != 1:
            return "用法：`/mmag skills [agent]`"
        if agent_name:
            try:
                agent = self.agent_registry.get(agent_name)
            except LookupError:
                return f"未找到已激活 Agent `{agent_name}`。"
            packages = tuple(agent.skills.values())
            title = f"### `{agent_name}` 可用 Skills"
        else:
            packages = self.skill_registry.list()
            title = "### 已激活 Skills"
        lines = [title]
        for package in sorted(packages, key=lambda item: item.manifest.metadata.ref):
            metadata = package.manifest.metadata
            lines.append(f"- `{metadata.ref}` — {metadata.description}")
        if len(lines) == 1:
            lines.append("当前没有可用的 Skill。")
        return "\n".join(lines)

    def _status(self, run_id: str, *, actor_id: str, channel_id: str) -> str:
        from ..control_plane import EntityType

        if run_id and len(run_id.split()) != 1:
            return "用法：`/mmag status [run-id]`"
        if run_id:
            entity_id = run_id if run_id.startswith("run:") else f"run:{run_id}"
            try:
                entity = self.store.get_lifecycle_entity(EntityType.AGENT_RUN, entity_id)
            except KeyError:
                return "未找到当前频道中属于你的运行记录。"
            entities = [entity]
        else:
            entities = self.store.list_lifecycle_entities_for_scope(
                EntityType.AGENT_RUN,
                channel_id,
                limit=50,
            )

        owned = [
            entity
            for entity in entities
            if entity.scope_id == channel_id
            and self._is_owned_run(entity.payload, actor_id, channel_id)
        ][:5]
        if not owned:
            return "未找到当前频道中属于你的运行记录。"
        lines = ["### 运行状态"]
        for entity in owned:
            snapshot = entity.payload.get("snapshot")
            agent_name = str(snapshot.get("agent_name") or "") if isinstance(snapshot, dict) else ""
            agent = f" · `{agent_name}`" if agent_name else ""
            lines.append(f"- `{entity.entity_id}` · **{entity.state}**{agent}")
        return "\n".join(lines)

    def _is_owned_run(
        self,
        payload: Any,
        actor_id: str,
        channel_id: str,
    ) -> bool:
        if not isinstance(payload, dict):
            payload = dict(payload)
        event_id = str(payload.get("inbox_event_id") or "")
        if not event_id:
            return False
        try:
            source = self.store.get_inbox(event_id).event
        except KeyError:
            return False
        return source.actor_id == actor_id and source.conversation_id == channel_id


class SlashCommandAdapter:
    """Authenticate Mattermost slash requests before command dispatch."""

    def __init__(self, token: str, service: SlashCommandService | None = None) -> None:
        if not token.strip():
            raise ValueError("MM_SLASH_COMMAND_TOKEN must not be empty")
        self._token = token
        self.service = service

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        supplied_token = str(payload.get("token") or "")
        if len(supplied_token.encode()) > _MAX_TOKEN_BYTES or not hmac.compare_digest(
            supplied_token, self._token
        ):
            raise PermissionError("invalid Mattermost slash command token")
        if str(payload.get("command") or "") != "/mmag":
            raise ValueError("unexpected Mattermost slash command")
        if not str(payload.get("user_id") or "") or not str(payload.get("channel_id") or ""):
            raise ValueError("Mattermost slash command actor and channel are required")

        text = str(payload.get("text") or "").strip()
        subcommand = text.split(maxsplit=1)[0].lower() if text else "help"
        if self.service is None:
            message = (
                _SLASH_COMMAND_HELP
                if subcommand == "help"
                else f"`{subcommand}` 子命令尚未开放。\n\n{_SLASH_COMMAND_HELP}"
            )
        else:
            message = await self.service.handle(payload)
        log_event(
            log,
            "mattermost.slash_command",
            status="completed",
            help_requested=subcommand == "help",
        )
        return {
            "response_type": "ephemeral",
            "text": message,
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
        owner_id: str = "",
    ) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("MM_ACTION_SIGNING_SECRET must contain at least 32 bytes")
        if ttl_seconds < 30 or ttl_seconds > 600:
            raise ValueError("Action token TTL must be between 30 and 600 seconds")
        self._secret = secret.encode()
        self.store = store
        self.ttl_seconds = ttl_seconds
        self._owner_id = owner_id.strip()

    def bind_owner(self, owner_id: str) -> None:
        owner_id = owner_id.strip()
        if not owner_id:
            raise ValueError("Action token owner must not be empty")
        if self._owner_id and self._owner_id != owner_id:
            raise RuntimeError("Action token owner is already bound")
        self._owner_id = owner_id

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
        owner_id = self._require_owner()
        now = time.time()
        jti = uuid.uuid4().hex
        expires_at = now + self.ttl_seconds
        claims: dict[str, str | int | float] = {
            "v": 2,
            "iss": owner_id,
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
        if not isinstance(raw, dict) or raw.get("v") != 2:
            raise ActionTokenError("Unsupported action token")
        claims = self._claims(raw)
        if not hmac.compare_digest(claims.issuer, self._require_owner()):
            raise ActionTokenError("Action token belongs to another Bot")
        now = time.time()
        if claims.expires_at < now:
            raise ActionTokenError("Action token has expired")
        return claims

    @staticmethod
    def _claims(raw: dict[str, Any]) -> ActionClaims:
        try:
            claims = ActionClaims(
                issuer=str(raw["iss"]),
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
                claims.issuer,
                claims.target,
                claims.scope_id,
                claims.conversation_id,
                claims.requested_by,
            )
        ):
            raise ActionTokenError("Incomplete action token")
        return claims

    def _require_owner(self) -> str:
        if not self._owner_id:
            raise ActionTokenError("Action token owner is not bound")
        return self._owner_id

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> str:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode()


class ActionCallbackServer:
    """Local callback gateway plus liveness/readiness probes."""

    def __init__(
        self,
        host: str,
        port: int,
        callback: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]] | None = None,
        *,
        path: str = "/actions",
        command_callback: Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]
        | None = None,
        command_path: str = "/integrations/commands",
        readiness: Callable[[], bool] | None = None,
    ) -> None:
        if callback is None and command_callback is None and readiness is None:
            raise ValueError("at least one callback route or readiness probe is required")
        self.host = host
        self.port = port
        self.path = path or "/actions"
        self.callback = callback
        self.command_path = command_path
        self.command_callback = command_callback
        self.readiness = readiness
        if callback is not None and command_callback is not None and self.path == command_path:
            raise ValueError("action and slash command callback paths must differ")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        readiness = self.readiness
        routes = {
            route_path: (route_callback, is_form)
            for route_path, route_callback, is_form in (
                (self.path, self.callback, False),
                (self.command_path, self.command_callback, True),
            )
            if route_callback is not None
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                path = urlsplit(self.path).path
                if path == "/health/live":
                    self._send_probe(200, "live")
                    return
                if path == "/health/ready" and readiness is not None:
                    ready = readiness()
                    self._send_probe(
                        200 if ready else 503,
                        "ready" if ready else "not_ready",
                    )
                    return
                self.send_error(404)

            def _send_probe(self, status_code: int, status: str) -> None:
                body = json.dumps({"status": status}).encode()
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
