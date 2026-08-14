"""Explicitly gated Mattermost API driver for black-box evaluation runs."""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from ..governance import SecretValue
from .models import (
    ControlPlaneObservation,
    EvaluationObservation,
    EvaluationProfile,
    EvaluationScenario,
    TaskObservation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_APPROVAL_ID = re.compile(r"(?:批准|approve)\s+`?([A-Za-z0-9_-]{8,128})`?", re.IGNORECASE)
_TERMINAL = frozenset({"succeeded", "failed", "exhausted"})


class EvaluationConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedActor:
    username: str
    password: SecretValue


@dataclass(frozen=True, slots=True)
class ResolvedMattermostProfile:
    base_url: str
    channel_id: str
    bot_username: str
    timeout_seconds: float
    poll_interval_seconds: float
    control_plane_db_path: str


class EvaluationEnvironment:
    """Resolve only explicitly referenced evaluation variables."""

    def __init__(self, values: Mapping[str, str] | None = None):
        self.values = values if values is not None else os.environ

    def resolve_profile(self, profile: EvaluationProfile) -> ResolvedMattermostProfile:
        if not self._enabled(profile.enabled_env):
            raise EvaluationConfigurationError(
                f"external evaluation requires {profile.enabled_env}=1"
            )
        base_url = self._required(profile.base_url_env).rstrip("/")
        self._validate_url(base_url)
        return ResolvedMattermostProfile(
            base_url=base_url,
            channel_id=self._required(profile.channel_id_env),
            bot_username=self._optional(profile.bot_username_env),
            timeout_seconds=profile.timeout_seconds,
            poll_interval_seconds=profile.poll_interval_seconds,
            control_plane_db_path=self._optional(profile.control_plane_db_env),
        )

    def resolve_actor(self, profile: EvaluationProfile, name: str) -> ResolvedActor:
        try:
            actor = profile.actors[name]
        except KeyError as error:
            raise EvaluationConfigurationError(
                f"evaluation profile {profile.id!r} has no actor {name!r}"
            ) from error
        return ResolvedActor(
            self._required(actor.username_env),
            SecretValue(self._required(actor.password_env)),
        )

    def _required(self, name: str) -> str:
        if not name:
            raise EvaluationConfigurationError("evaluation environment reference is empty")
        value = str(self.values.get(name, "")).strip()
        if not value:
            raise EvaluationConfigurationError(f"required evaluation variable {name} is missing")
        return value

    def _optional(self, name: str) -> str:
        return str(self.values.get(name, "")).strip() if name else ""

    def _enabled(self, name: str) -> bool:
        return self._optional(name).lower() in {"1", "true", "yes"}

    @staticmethod
    def _validate_url(value: str) -> None:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or not parsed.hostname:
            raise EvaluationConfigurationError("Mattermost evaluation URL is invalid")
        if parsed.scheme == "https":
            return
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return
        raise EvaluationConfigurationError(
            "credentialed Mattermost evaluation requires HTTPS or trusted localhost HTTP"
        )


class MattermostUserSession:
    def __init__(self, base_url: str, actor: ResolvedActor):
        self.actor = actor
        self.client = httpx.AsyncClient(
            base_url=f"{base_url}/api/v4",
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        )
        self.user_id = ""

    async def __aenter__(self) -> MattermostUserSession:
        entered = False
        try:
            response = await self.client.post(
                "/users/login",
                json={"login_id": self.actor.username, "password": self.actor.password.reveal()},
            )
            response.raise_for_status()
            token = response.headers.get("Token", "")
            if not token:
                raise EvaluationConfigurationError("Mattermost login returned no session token")
            self.client.headers["Authorization"] = f"Bearer {token}"
            payload = response.json()
            self.user_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
            entered = True
            return self
        finally:
            if not entered:
                await self.client.aclose()

    async def __aexit__(self, *_args: object) -> None:
        try:
            await self.client.post("/users/logout")
        finally:
            await self.client.aclose()

    async def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str = "",
        props: Mapping[str, Any] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "message": message,
            "pending_post_id": uuid.uuid4().hex,
        }
        if root_id:
            payload["root_id"] = root_id
        if props:
            payload["props"] = dict(props)
        response = await self.client.post("/posts", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("Mattermost create-post response has no post ID")
        return result

    async def get_thread(self, root_post_id: str) -> tuple[dict[str, Any], ...]:
        response = await self.client.get(f"/posts/{root_post_id}/thread")
        response.raise_for_status()
        payload = response.json()
        order = payload.get("order", []) if isinstance(payload, dict) else []
        posts = payload.get("posts", {}) if isinstance(payload, dict) else {}
        return tuple(
            dict(posts[post_id])
            for post_id in order
            if isinstance(posts, dict) and isinstance(posts.get(post_id), dict)
        )


class SQLiteEvaluationObserver:
    """Read only stable lifecycle states; never mutates a target deployment."""

    def task_ids(self, path: str) -> frozenset[str]:
        if not path:
            return frozenset()
        database = Path(path).resolve()
        if not database.is_file():
            return frozenset()
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return frozenset(str(row[0]) for row in connection.execute("SELECT id FROM tasks"))
        except sqlite3.Error:
            return frozenset()
        finally:
            connection.close()

    def approval_registered(self, path: str, approval_id: str, root_post_id: str) -> bool:
        if not path or not approval_id or not root_post_id:
            return False
        database = Path(path).resolve()
        if not database.is_file():
            return False
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute(
                """SELECT 1 FROM approval_requests
                WHERE id=? AND json_extract(arguments, '$.thread_id')=?
                LIMIT 1""",
                (approval_id, f"mattermost:{root_post_id}"),
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False
        finally:
            connection.close()

    def observe(
        self,
        path: str,
        root_post_id: str,
        *,
        baseline_task_ids: frozenset[str] = frozenset(),
        requester_id: str = "",
        channel_id: str = "",
    ) -> tuple[ControlPlaneObservation, tuple[TaskObservation, ...]]:
        if not path:
            return ControlPlaneObservation(), ()
        database = Path(path).resolve()
        if not database.is_file():
            return ControlPlaneObservation(), ()
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            run = self._state(connection, "agent_run", f"run:{root_post_id}")
            task = self._state(connection, "task", f"task:{root_post_id}")
            deliveries = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT status FROM outbox_deliveries WHERE agent_run_id=? ORDER BY created_at",
                    (f"run:{root_post_id}",),
                ).fetchall()
            )
            delivery_post_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    """SELECT remote_id FROM outbox_deliveries
                    WHERE root_id=? AND status='delivered' AND remote_id<>''
                    ORDER BY created_at""",
                    (root_post_id,),
                ).fetchall()
            )
            trace_id, agent_name = self._agent_trace(connection, root_post_id)
            capabilities = tuple(
                sorted(
                    {
                        str(row[0])
                        for row in connection.execute(
                            """SELECT target FROM audit_events
                            WHERE trace_id=? AND event_type IN ('policy.decision', 'runtime.tool.call')
                            ORDER BY created_at""",
                            (trace_id,),
                        ).fetchall()
                        if row[0]
                    }
                )
            )
            tasks = self._created_tasks(
                connection,
                baseline_task_ids,
                requester_id=requester_id,
                channel_id=channel_id,
            )
            return ControlPlaneObservation(
                run,
                task,
                deliveries,
                agent_name,
                capabilities,
                delivery_post_ids,
            ), tasks
        except sqlite3.Error:
            return ControlPlaneObservation(), ()
        finally:
            connection.close()

    @staticmethod
    def _state(connection: sqlite3.Connection, entity_type: str, entity_id: str) -> str:
        row = connection.execute(
            "SELECT state FROM lifecycle_entities WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        ).fetchone()
        return str(row[0]) if row else ""

    @staticmethod
    def _agent_trace(connection: sqlite3.Connection, root_post_id: str) -> tuple[str, str]:
        row = connection.execute(
            """SELECT trace_id, target FROM audit_events
            WHERE event_type='agent.run'
              AND json_extract(details, '$.message_id')=?
            ORDER BY created_at DESC LIMIT 1""",
            (root_post_id,),
        ).fetchone()
        return (str(row[0] or ""), str(row[1] or "")) if row else ("", "")

    @staticmethod
    def _created_tasks(
        connection: sqlite3.Connection,
        baseline_task_ids: frozenset[str],
        *,
        requester_id: str,
        channel_id: str,
    ) -> tuple[TaskObservation, ...]:
        rows = connection.execute(
            """SELECT id, title, scope_id, creator_id, channel_id, execution_key
            FROM tasks ORDER BY created_at"""
        ).fetchall()
        return tuple(
            TaskObservation(
                id=str(row["id"]),
                title=str(row["title"]),
                scope_id=str(row["scope_id"]),
                creator_matches_requester=str(row["creator_id"]) == requester_id,
                channel_matches_request=str(row["channel_id"]) == channel_id,
                execution_key_present=bool(str(row["execution_key"])),
            )
            for row in rows
            if str(row["id"]) not in baseline_task_ids
            and (not requester_id or str(row["creator_id"]) == requester_id)
            and (not channel_id or str(row["channel_id"]) == channel_id)
        )


class MattermostEvaluationDriver:
    def __init__(
        self,
        *,
        allow_external: bool = False,
        environment: EvaluationEnvironment | None = None,
        control_plane_observer: SQLiteEvaluationObserver | None = None,
    ) -> None:
        self.allow_external = allow_external
        self.environment = environment or EvaluationEnvironment()
        self.control_plane_observer = control_plane_observer or SQLiteEvaluationObserver()

    async def execute(
        self,
        scenario: EvaluationScenario,
        profile: EvaluationProfile,
        evaluation_run_id: str,
    ) -> EvaluationObservation:
        if not self.allow_external:
            raise PermissionError("external evaluation requires an explicit allow_external flag")
        if profile.driver != "mattermost-api":
            raise EvaluationConfigurationError(
                f"Mattermost driver cannot execute profile type {profile.driver!r}"
            )
        resolved = self.environment.resolve_profile(profile)
        requester = self.environment.resolve_actor(profile, scenario.actor)
        baseline_task_ids = self.control_plane_observer.task_ids(
            resolved.control_plane_db_path
        )
        started = time.monotonic()
        async with AsyncExitStack() as stack:
            requester_session = await stack.enter_async_context(
                MattermostUserSession(resolved.base_url, requester)
            )
            if "{bot}" in scenario.message and not resolved.bot_username:
                raise EvaluationConfigurationError(
                    f"scenario {scenario.id!r} requires {profile.bot_username_env}"
                )
            bot_username = resolved.bot_username.removeprefix("@")
            message = (
                scenario.message.replace("{bot}", bot_username)
                .replace("{eval_id}", evaluation_run_id)
                .strip()
            )
            root = await requester_session.create_post(
                resolved.channel_id,
                message,
                props={"mmag_eval_run_id": evaluation_run_id, "mmag_eval_case_id": scenario.id},
            )
            root_post_id = str(root["id"])
            return await self._observe_and_act(
                stack,
                requester_session,
                scenario,
                profile,
                resolved,
                root_post_id,
                started,
                baseline_task_ids,
            )

    async def _observe_and_act(
        self,
        stack: AsyncExitStack,
        requester: MattermostUserSession,
        scenario: EvaluationScenario,
        profile: EvaluationProfile,
        resolved: ResolvedMattermostProfile,
        root_post_id: str,
        started: float,
        baseline_task_ids: frozenset[str],
    ) -> EvaluationObservation:
        deadline = time.monotonic() + resolved.timeout_seconds
        approval = scenario.expected.get("approval", {})
        approval = dict(approval) if isinstance(approval, dict) else {}
        decision = str(approval.get("decision") or "")
        authorized = approval.get("authorized") is not False
        decision_sent = False
        approval_id = ""
        decision_session: MattermostUserSession | None = None
        latest_posts: tuple[dict[str, Any], ...] = ()

        while time.monotonic() < deadline:
            latest_posts = await requester.get_thread(root_post_id)
            bot_posts = self._bot_posts(latest_posts)
            approval_post = self._latest_with_status(bot_posts, "waiting_approval")
            if approval_post is not None:
                candidate_id = self._approval_id(str(approval_post.get("message") or ""))
                locally_registered = not resolved.control_plane_db_path or (
                    self.control_plane_observer.approval_registered(
                        resolved.control_plane_db_path,
                        candidate_id,
                        root_post_id,
                    )
                )
                if candidate_id and locally_registered:
                    approval_id = candidate_id
                if decision and not decision_sent and approval_id:
                    actor_name = str(approval.get("actor") or scenario.actor)
                    if actor_name == scenario.actor:
                        decision_session = requester
                    else:
                        actor = self.environment.resolve_actor(profile, actor_name)
                        decision_session = await stack.enter_async_context(
                            MattermostUserSession(resolved.base_url, actor)
                        )
                    verb = "批准" if decision == "approve" else "拒绝"
                    await decision_session.create_post(
                        resolved.channel_id,
                        f"@{resolved.bot_username.removeprefix('@')} {verb} {approval_id}",
                        root_id=root_post_id,
                    )
                    decision_sent = True
                elif not decision and approval_id:
                    return self._observation(
                        latest_posts,
                        root_post_id,
                        started,
                        approval_id=approval_id,
                        control_plane_path=resolved.control_plane_db_path,
                        baseline_task_ids=baseline_task_ids,
                        requester_id=requester.user_id,
                        channel_id=resolved.channel_id,
                    )

            terminal = self._latest_terminal(bot_posts)
            if terminal is not None and (
                not approval or not decision or decision_sent or approval_post is None
            ):
                artifacts = scenario.expected.get("artifacts", {})
                minimum_artifacts = (
                    int(artifacts.get("minimum", 0)) if isinstance(artifacts, dict) else 0
                )
                if self._artifact_count(bot_posts) < minimum_artifacts:
                    await asyncio.sleep(resolved.poll_interval_seconds)
                    continue
                denied = bool(decision_sent and not authorized and self._kind(terminal) == "error")
                observation = self._observation(
                    latest_posts,
                    root_post_id,
                    started,
                    approval_id=approval_id,
                    approval_denied=denied,
                    control_plane_path=resolved.control_plane_db_path,
                    baseline_task_ids=baseline_task_ids,
                    requester_id=requester.user_id,
                    channel_id=resolved.channel_id,
                )
                if self._control_plane_ready(scenario, observation):
                    return observation
            await asyncio.sleep(resolved.poll_interval_seconds)

        return self._observation(
            latest_posts,
            root_post_id,
            started,
            approval_id=approval_id,
            timed_out=True,
            control_plane_path=resolved.control_plane_db_path,
            baseline_task_ids=baseline_task_ids,
            requester_id=requester.user_id,
            channel_id=resolved.channel_id,
        )

    @staticmethod
    def _control_plane_ready(
        scenario: EvaluationScenario,
        observation: EvaluationObservation,
    ) -> bool:
        expected = scenario.expected
        control_plane = expected.get("control_plane", {})
        control_plane = dict(control_plane) if isinstance(control_plane, dict) else {}
        actual = observation.control_plane
        if control_plane.get("agent_run_state") != actual.agent_run_state and control_plane.get(
            "agent_run_state"
        ):
            return False
        if control_plane.get("task_state") != actual.task_state and control_plane.get("task_state"):
            return False
        if control_plane.get("agent_name") != actual.agent_name and control_plane.get("agent_name"):
            return False
        delivery_states = control_plane.get("delivery_states_all", ())
        if isinstance(delivery_states, list) and not set(delivery_states).issubset(
            actual.delivery_states
        ):
            return False

        capabilities = expected.get("capabilities", {})
        capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
        capability_names = capabilities.get("contains_all", ())
        if isinstance(capability_names, list) and not set(capability_names).issubset(
            actual.capability_names
        ):
            return False

        tasks = expected.get("tasks", {})
        tasks = dict(tasks) if isinstance(tasks, dict) else {}
        created = observation.created_tasks
        if tasks.get("minimum_created") is not None and len(created) < int(
            tasks["minimum_created"]
        ):
            return False
        title_contains = str(tasks.get("title_contains") or "")
        if title_contains and not any(title_contains in task.title for task in created):
            return False
        if tasks.get("requester_is_creator") and (
            not created or not all(task.creator_matches_requester for task in created)
        ):
            return False
        if tasks.get("current_channel") and (
            not created or not all(task.channel_matches_request for task in created)
        ):
            return False
        return not tasks.get("execution_key_required") or (
            bool(created) and all(task.execution_key_present for task in created)
        )

    def _observation(
        self,
        posts: tuple[dict[str, Any], ...],
        root_post_id: str,
        started: float,
        *,
        approval_id: str = "",
        approval_denied: bool = False,
        timed_out: bool = False,
        control_plane_path: str = "",
        baseline_task_ids: frozenset[str] = frozenset(),
        requester_id: str = "",
        channel_id: str = "",
    ) -> EvaluationObservation:
        all_bot_posts = self._bot_posts(posts)
        control_plane, created_tasks = self.control_plane_observer.observe(
            control_plane_path,
            root_post_id,
            baseline_task_ids=baseline_task_ids,
            requester_id=requester_id,
            channel_id=channel_id,
        )
        local_post_ids = frozenset(control_plane.delivery_post_ids)
        bot_posts = (
            tuple(post for post in all_bot_posts if str(post.get("id") or "") in local_post_ids)
            if local_post_ids
            else all_bot_posts
        )
        terminal = self._latest_terminal(bot_posts)
        approval_seen = self._latest_with_status(bot_posts, "waiting_approval") is not None
        visible = [
            str(post.get("message") or "")
            for post in bot_posts
            if self._kind(post) not in {"status", "approval"}
        ]
        return EvaluationObservation(
            root_post_id=root_post_id,
            run_id=f"mattermost:{root_post_id}",
            response_text="\n\n".join(value for value in visible if value),
            response_kind=self._kind(terminal) if terminal is not None else "approval",
            terminal_status=self._status(terminal) if terminal is not None else "waiting_approval",
            thread_consistent=all(
                str(post.get("root_id") or "") == root_post_id for post in bot_posts
            ),
            approval_seen=approval_seen,
            approval_id=approval_id,
            approval_decision_denied=approval_denied,
            artifact_count=self._artifact_count(bot_posts),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            post_ids=tuple(str(post.get("id") or "") for post in bot_posts),
            control_plane=control_plane,
            created_tasks=created_tasks,
        )

    @staticmethod
    def _bot_posts(posts: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return tuple(
            post
            for post in posts
            if isinstance(post.get("props"), dict)
            and str(post["props"].get("from_bot", "")).lower() == "true"
        )

    @staticmethod
    def _artifact_count(posts: tuple[dict[str, Any], ...]) -> int:
        artifacts: set[str] = set()
        for post in posts:
            artifacts.update(str(item) for item in post.get("file_ids", ()) if item)
            metadata = post.get("metadata", {})
            files = metadata.get("files", ()) if isinstance(metadata, dict) else ()
            for item in files if isinstance(files, list) else ():
                if isinstance(item, dict) and item.get("id"):
                    artifacts.add(str(item["id"]))
        return len(artifacts)

    @classmethod
    def _latest_terminal(cls, posts: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
        matches = [post for post in posts if cls._status(post) in _TERMINAL]
        return max(matches, key=lambda item: int(item.get("create_at") or 0)) if matches else None

    @classmethod
    def _latest_with_status(
        cls, posts: tuple[dict[str, Any], ...], status: str
    ) -> dict[str, Any] | None:
        matches = [post for post in posts if cls._status(post) == status]
        return max(matches, key=lambda item: int(item.get("create_at") or 0)) if matches else None

    @staticmethod
    def _status(post: dict[str, Any] | None) -> str:
        props = post.get("props", {}) if isinstance(post, dict) else {}
        return str(props.get("mmag_status") or "") if isinstance(props, dict) else ""

    @staticmethod
    def _kind(post: dict[str, Any] | None) -> str:
        props = post.get("props", {}) if isinstance(post, dict) else {}
        return str(props.get("mmag_kind") or "") if isinstance(props, dict) else ""

    @staticmethod
    def _approval_id(message: str) -> str:
        match = _APPROVAL_ID.search(message)
        return match.group(1) if match else ""
