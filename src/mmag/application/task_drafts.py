"""Meeting action-item draft workflow and explicit human confirmation."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..capabilities import CapabilityContext, CapabilityStatus, bind_capability_context
from ..control_plane import Scope, TaskDraft, TaskDraftState
from ..logger import get_logger, log_context, log_event
from .views import ResponseAction, ResponseKind, ResponseSection, ResponseView, RunStatus

if TYPE_CHECKING:
    from ..agent_system import AgentOutput
    from ..control_plane import MattermostAccessGuard, TaskDraftStore
    from ..memory import Memory

log = get_logger(__name__)
_COMMAND = re.compile(r"^(确认|放弃)任务草案\s+([0-9a-f]{32})$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DUE_AT = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\+08:00"
)


class TaskDraftCoordinator:
    """Persist meeting action items and commit them through create_task."""

    def __init__(
        self,
        *,
        store: TaskDraftStore,
        memory: Memory,
        mm_client,
        capability_registry,
        access_guard: MattermostAccessGuard,
        scope_resolver,
        action_tokens,
        audit_store,
    ) -> None:
        self.store = store
        self.memory = memory
        self.mm = mm_client
        self.capability_registry = capability_registry
        self.access_guard = access_guard
        self.scope_resolver = scope_resolver
        self.action_tokens = action_tokens
        self.audit_store = audit_store

    async def attach(self, post: dict, output: AgentOutput, view: ResponseView) -> ResponseView:
        result = output.result if isinstance(output.result, dict) else {}
        if (
            output.agent_name != "report"
            or "message_range" not in result
            or view.status is not RunStatus.SUCCEEDED
        ):
            return view
        scope = self.scope_resolver.resolve_post(post)
        members, directory_available = await self._scope_members(post, scope)
        reference_time = self._post_time(post)
        items, rejected_count = self._items(
            result,
            post=post,
            scope=scope,
            members=members,
            directory_available=directory_available,
            reference_time=reference_time,
        )
        if not items:
            warning = "行动项缺少当前会话内可验证的来源，因此未生成任务草案。"
            return replace(view, warnings=(*view.warnings, warning))
        encoded = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        draft = self.store.create(
            installation_id=scope.installation_id,
            tenant_id=scope.tenant_id,
            scope_id=scope.id,
            channel_id=str(post.get("channel_id") or ""),
            root_id=str(post.get("root_id") or post.get("id") or ""),
            run_id=view.run_id,
            requested_by=str(post.get("user_id") or ""),
            title=str(result.get("title") or "会议纪要"),
            items=items,
            content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        )
        self.audit_store.append_audit(
            "task_draft.created",
            actor_id=draft.requested_by,
            scope_id=draft.scope_id,
            trace_id=draft.run_id,
            target=draft.id,
            decision="drafted",
            details={
                "schema_version": "1.0",
                "item_count": len(draft.items),
                "person_match_counts": self._match_counts(draft.items),
            },
        )
        log_event(
            log,
            "task_draft.created",
            status="completed",
            draft_id=draft.id,
            item_count=len(draft.items),
            rejected_item_count=rejected_count,
            member_count=len(members),
            person_match_counts=self._match_counts(draft.items),
        )
        commands = (
            f"确认创建：`确认任务草案 {draft.id}`",
            f"放弃草案：`放弃任务草案 {draft.id}`",
            "确认后会写入唯一负责人候选和可确定的截止时间；有歧义的字段保持为空，不会自动 @ 人员。",
        )
        warnings = view.warnings
        if rejected_count:
            warnings = (
                *warnings,
                f"有 {rejected_count} 个行动项缺少当前会话内可验证来源，未纳入草案。",
            )
        if not directory_available:
            warnings = (*warnings, "当前成员目录暂不可用，相关人物已保留但未进行候选匹配。")
        return replace(
            view,
            sections=(
                *view.sections,
                ResponseSection(
                    "人物候选（系统按当前频道成员目录核对）",
                    items=self._person_lines(draft.items),
                ),
                ResponseSection("任务草案", items=commands),
            ),
            warnings=warnings,
            actions=self._actions(draft),
        )

    @staticmethod
    def parse_command(message: str, *, bot_username: str = "") -> tuple[bool, str] | None:
        normalized = message.strip()
        mention = f"@{bot_username}" if bot_username else ""
        if mention and normalized.lower().startswith(mention.lower()):
            normalized = normalized[len(mention) :].strip()
        matched = _COMMAND.fullmatch(normalized)
        if matched is None:
            return None
        return matched.group(1) == "确认", matched.group(2)

    async def decide(
        self,
        draft_id: str,
        *,
        actor_id: str,
        scope: Scope,
        approved: bool,
    ) -> tuple[TaskDraft, str]:
        draft = self.store.get(draft_id)
        self._validate_context(draft, actor_id=actor_id, scope=scope)
        if approved and draft.state is TaskDraftState.COMMITTED:
            log_event(
                log,
                "task_draft.replayed",
                status="completed",
                draft_id=draft.id,
                task_count=len(draft.task_ids),
            )
            return draft, f"任务草案已创建过，共 {len(draft.task_ids)} 个任务。"
        if not approved and draft.state is TaskDraftState.REJECTED:
            log_event(
                log,
                "task_draft.replayed",
                status="completed",
                draft_id=draft.id,
                task_count=0,
            )
            return draft, "任务草案已经放弃，未创建任何任务。"
        await self.access_guard.require(
            actor_id,
            scope.id,
            channel_id=scope.conversation_id,
        )
        if not approved:
            rejected = self.store.reject(draft_id, actor_id=actor_id)
            self._audit(rejected, actor_id=actor_id, decision="rejected")
            log_event(log, "task_draft.rejected", status="completed", draft_id=draft_id)
            return rejected, "已放弃任务草案，未创建任何任务。"

        claimed = self.store.claim_commit(draft_id, actor_id=actor_id)
        if claimed.state is TaskDraftState.COMMITTED:
            return claimed, f"任务草案已创建过，共 {len(claimed.task_ids)} 个任务。"
        self._audit(
            claimed,
            actor_id=actor_id,
            decision="approved",
            details={"item_count": len(claimed.items)},
        )
        task_ids: list[str] = []
        created_tasks: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(claimed.items):
                task = await self._create_task(claimed, item, index=index, scope=scope)
                task_ids.append(str(task["id"]))
                created_tasks.append(task)
        except Exception as error:
            self._audit(
                claimed,
                actor_id=actor_id,
                decision="failed",
                details={"error_code": type(error).__name__},
            )
            log_event(
                log,
                "task_draft.commit_failed",
                level=40,
                status="failed",
                draft_id=draft_id,
                error_code=type(error).__name__,
            )
            raise
        committed = self.store.complete(
            draft_id,
            actor_id=actor_id,
            task_ids=tuple(task_ids),
        )
        self._audit(
            committed,
            actor_id=actor_id,
            decision="committed",
            details={"task_count": len(task_ids)},
        )
        log_event(
            log,
            "task_draft.committed",
            status="completed",
            draft_id=draft_id,
            task_count=len(task_ids),
        )
        assigned = sum(bool(task.get("assignee_id")) for task in created_tasks)
        scheduled = sum(bool(task.get("due_time")) for task in created_tasks)
        return committed, self._commit_message(len(task_ids), assigned, scheduled)

    def result_view(self, draft: TaskDraft, message: str) -> ResponseView:
        committed = draft.state is TaskDraftState.COMMITTED
        items = tuple(
            self._task_result_line(
                task_id,
                item,
                self.memory.repositories.tasks.get(task_id, scope_id=draft.scope_id),
            )
            for task_id, item in zip(draft.task_ids, draft.items, strict=False)
        )
        return ResponseView(
            kind=ResponseKind.RESULT,
            title="任务草案已确认" if committed else "任务草案已放弃",
            summary=message,
            status=RunStatus.SUCCEEDED,
            run_id=draft.run_id,
            sections=(ResponseSection("已创建任务", items=items),) if items else (),
        )

    @staticmethod
    def _validate_context(draft: TaskDraft, *, actor_id: str, scope: Scope) -> None:
        if draft.requested_by != actor_id:
            raise PermissionError("只有草案创建者可以处理该草案")
        if (
            draft.scope_id != scope.id
            or draft.channel_id != scope.conversation_id
            or draft.installation_id != scope.installation_id
            or draft.tenant_id != scope.tenant_id
        ):
            raise PermissionError("任务草案属于其他会话或租户")

    async def _create_task(
        self,
        draft: TaskDraft,
        item: dict[str, Any],
        *,
        index: int,
        scope: Scope,
    ) -> dict[str, Any]:
        related_person = str(item.get("related_person") or "未提及")
        person_match = item.get("person_match")
        match_text = self._match_description(person_match if isinstance(person_match, dict) else {})
        due_text = str(item.get("due_text") or "未提及")
        due_candidate = item.get("due_candidate")
        due_description = self._due_description(
            due_candidate if isinstance(due_candidate, dict) else {}
        )
        assignee_id = await self._authorized_assignee(draft, item)
        due_time = self._resolved_due_time(item)
        source_ids = tuple(str(value) for value in item.get("source_post_ids") or ())
        description = (
            f"相关人物：{related_person}\n"
            f"人物匹配：{match_text}\n"
            f"截止时间原文：{due_text}\n"
            f"截止时间确认：{due_description}\n"
            f"来源 Post：{', '.join(source_ids)}\n"
            f"任务草案：{draft.id}"
        )
        context = CapabilityContext(
            trace_id=log_context.get("trace_id", draft.run_id),
            actor_id=draft.requested_by,
            conversation_id=draft.channel_id,
            message_id=draft.root_id,
            message="确认会议任务草案",
            scope=draft.scope_id,
            allowed_capabilities=frozenset({"create_task"}),
            run_id=draft.run_id,
            installation_id=scope.installation_id,
            tenant_id=scope.tenant_id,
            scope_kind=scope.kind.value,
            owner_id=scope.owner_id,
            team_id=scope.team_id,
            channel_type=scope.channel_type,
            execution_key=f"task-draft:{draft.id}:{index}",
        )
        with bind_capability_context(context):
            result = await self.capability_registry.execute(
                "create_task",
                {
                    "title": str(item["content"]),
                    "assignee_id": assignee_id,
                    "description": description,
                    "task_type": "meeting",
                    "due_time": due_time,
                    "priority": 1,
                },
                preauthorized=True,
            )
        if result.status is not CapabilityStatus.SUCCESS or not isinstance(result.data, dict):
            raise RuntimeError(result.message or "创建任务失败")
        if result.data.get("status") != "ok" or not isinstance(result.data.get("task"), dict):
            raise RuntimeError(str(result.data.get("error") or "创建任务失败"))
        return dict(result.data["task"])

    def _items(
        self,
        result: dict[str, Any],
        *,
        post: dict,
        scope: Scope,
        members: tuple[dict[str, Any], ...],
        directory_available: bool,
        reference_time: datetime,
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        raw_items = result.get("action_items")
        candidates = raw_items if isinstance(raw_items, list) else []
        all_ids: list[str] = []
        for item in candidates[:50]:
            if isinstance(item, dict) and isinstance(item.get("source_post_ids"), list):
                all_ids.extend(str(value) for value in item["source_post_ids"][:50])
        verified = self.memory.repositories.messages.verified_source_ids(
            tuple(all_ids),
            conversation_id=str(post.get("channel_id") or ""),
            scope_id=scope.id,
        )
        items: list[dict[str, Any]] = []
        rejected = 0
        for raw in candidates[:50]:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            content = str(raw.get("content") or "").strip()[:500]
            refs = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (raw.get("source_post_ids") or ())[:50]
                    if str(value) in verified
                )
            )
            if not content or not refs:
                rejected += 1
                continue
            related = str(raw.get("owner_username") or "").strip().lstrip("@")[:128]
            due = str(raw.get("due_text") or "").strip()[:128]
            due_at = str(raw.get("due_at") or "").strip()[:64]
            due_text = due or "未提及"
            items.append(
                {
                    "content": content,
                    "related_person": related or "未提及",
                    "due_text": due_text,
                    "source_post_ids": refs,
                    "confirmation_version": 1,
                    "person_match": self._match_person(
                        related or "未提及",
                        members,
                        directory_available=directory_available,
                    ),
                    "due_candidate": self._due_candidate(
                        due_text,
                        due_at=due_at,
                        reference_time=reference_time,
                    ),
                }
            )
        return tuple(items), rejected

    async def _scope_members(
        self, post: dict, scope: Scope
    ) -> tuple[tuple[dict[str, Any], ...], bool]:
        actor_id = str(post.get("user_id") or "")
        try:
            await self.access_guard.require(
                actor_id,
                scope.id,
                channel_id=scope.conversation_id,
            )
            users = await self.mm.list_channel_users_async(scope.conversation_id)
        except Exception as error:
            log_event(
                log,
                "task_draft.person_directory_failed",
                level=30,
                status="unavailable",
                error_code=type(error).__name__,
            )
            return (), False
        members = tuple(
            self._member_record(user)
            for user in users
            if str(user.get("id") or "")
            and not bool(user.get("is_bot"))
            and not int(user.get("delete_at") or 0)
        )
        return members, True

    @classmethod
    def _match_person(
        cls,
        mention: str,
        members: tuple[dict[str, Any], ...],
        *,
        directory_available: bool,
    ) -> dict[str, Any]:
        normalized = cls._normalize_person(mention)
        if not directory_available:
            return {"mention": mention, "status": "unavailable", "candidates": []}
        if not normalized or mention == "未提及":
            return {"mention": mention, "status": "unresolved", "candidates": []}
        candidates = [
            member
            for member in members
            if normalized
            in {
                cls._normalize_person(member.get("username")),
                cls._normalize_person(member.get("nickname")),
                cls._normalize_person(member.get("display_name")),
            }
        ]
        candidates.sort(key=lambda item: (str(item.get("username") or ""), str(item["user_id"])))
        status = "resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved"
        return {"mention": mention, "status": status, "candidates": candidates[:10]}

    @classmethod
    def _member_record(cls, user: dict[str, Any]) -> dict[str, str]:
        first_name = cls._plain(user.get("first_name"))
        last_name = cls._plain(user.get("last_name"))
        full_name = " ".join(value for value in (first_name, last_name) if value)
        return {
            "user_id": str(user.get("id") or ""),
            "username": cls._plain(user.get("username")),
            "nickname": cls._plain(user.get("nickname")),
            "display_name": full_name or cls._plain(user.get("nickname")),
        }

    @classmethod
    def _person_lines(cls, items: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
        lines: list[str] = []
        for item in items:
            match = item.get("person_match")
            match = match if isinstance(match, dict) else {}
            lines.append(
                f"{cls._plain(item.get('content'))}：{cls._plain(item.get('related_person'))}"
                f" → {cls._match_description(match)}；{cls._due_description_for_item(item)}"
            )
        return tuple(lines)

    @classmethod
    def _match_description(cls, match: dict[str, Any]) -> str:
        candidates = match.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        labels = [cls._candidate_label(item) for item in candidates if isinstance(item, dict)]
        status = str(match.get("status") or "unresolved")
        if status == "resolved" and labels:
            return f"唯一候选 {labels[0]}"
        if status == "ambiguous" and labels:
            return f"多个候选：{'、'.join(labels)}"
        if status == "unavailable":
            return "成员目录不可用"
        return "未匹配到当前频道成员"

    @classmethod
    def _candidate_label(cls, candidate: dict[str, Any]) -> str:
        username = cls._plain(candidate.get("username")) or "未知用户"
        display_name = cls._plain(candidate.get("display_name"))
        return f"`{username}`（{display_name}）" if display_name and display_name != username else f"`{username}`"

    @classmethod
    def _normalize_person(cls, value: Any) -> str:
        return "".join(cls._plain(value).lstrip("@").casefold().split())

    @staticmethod
    def _plain(value: Any) -> str:
        return " ".join(str(value or "").replace("`", "'").split())[:128]

    @staticmethod
    def _match_counts(items: tuple[dict[str, Any], ...]) -> dict[str, int]:
        counts = {"resolved": 0, "ambiguous": 0, "unresolved": 0, "unavailable": 0}
        for item in items:
            match = item.get("person_match")
            status = str(match.get("status") or "unresolved") if isinstance(match, dict) else "unresolved"
            if status in counts:
                counts[status] += 1
        return counts

    @classmethod
    def _due_candidate(
        cls,
        value: str,
        *,
        due_at: str,
        reference_time: datetime,
    ) -> dict[str, Any]:
        raw = cls._plain(value)
        normalized = due_at.strip()
        if not raw or raw == "未提及" or _DUE_AT.fullmatch(normalized) is None:
            return {"raw": raw or "未提及", "status": "unresolved", "due_time": 0, "display": ""}
        try:
            candidate = datetime.fromisoformat(normalized)
        except ValueError:
            return {"raw": raw, "status": "unresolved", "due_time": 0, "display": ""}
        if candidate.utcoffset() != timedelta(hours=8) or candidate <= reference_time.astimezone(
            _SHANGHAI
        ):
            return {"raw": raw, "status": "unresolved", "due_time": 0, "display": ""}
        candidate = candidate.astimezone(_SHANGHAI)
        return {
            "raw": raw,
            "status": "resolved",
            "due_time": candidate.timestamp(),
            "display": candidate.strftime("%Y-%m-%d %H:%M"),
        }

    @staticmethod
    def _post_time(post: dict) -> datetime:
        raw = post.get("create_at")
        try:
            raw_timestamp = float(str(raw))
            timestamp = (
                raw_timestamp / 1000
                if raw_timestamp > 10_000_000_000
                else raw_timestamp
            )
        except (TypeError, ValueError):
            timestamp = time.time()
        return datetime.fromtimestamp(timestamp, tz=_SHANGHAI)

    @staticmethod
    def _resolved_assignee(item: dict[str, Any]) -> str:
        if item.get("confirmation_version") != 1:
            return ""
        match = item.get("person_match")
        if not isinstance(match, dict) or match.get("status") != "resolved":
            return ""
        candidates = match.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            return ""
        candidate = candidates[0]
        return str(candidate.get("user_id") or "") if isinstance(candidate, dict) else ""

    @staticmethod
    def _resolved_due_time(item: dict[str, Any]) -> float:
        if item.get("confirmation_version") != 1:
            return 0
        candidate = item.get("due_candidate")
        if not isinstance(candidate, dict) or candidate.get("status") != "resolved":
            return 0
        try:
            return max(float(candidate.get("due_time") or 0), 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _due_description_for_item(cls, item: dict[str, Any]) -> str:
        candidate = item.get("due_candidate")
        return f"截止时间候选：{cls._due_description(candidate if isinstance(candidate, dict) else {})}"

    @classmethod
    def _due_description(cls, candidate: dict[str, Any]) -> str:
        if candidate.get("status") == "resolved" and candidate.get("display"):
            return cls._plain(candidate["display"])
        return "未确定"

    @classmethod
    def _task_result_line(
        cls,
        task_id: str,
        item: dict[str, Any],
        task: dict[str, Any] | None,
    ) -> str:
        persisted = task or {}
        match = item.get("person_match")
        match = match if isinstance(match, dict) else {}
        candidates = match.get("candidates")
        due_candidate = item.get("due_candidate")
        due_candidate = due_candidate if isinstance(due_candidate, dict) else {}
        assignee = (
            cls._candidate_label(candidates[0])
            if persisted.get("assignee_id")
            and isinstance(candidates, list)
            and candidates
            and isinstance(candidates[0], dict)
            else "未分配"
        )
        due = (
            cls._due_description(due_candidate)
            if persisted.get("due_time")
            else "未确定"
        )
        return f"`{task_id}`；负责人：{assignee}；截止：{due}"

    async def _authorized_assignee(self, draft: TaskDraft, item: dict[str, Any]) -> str:
        assignee_id = self._resolved_assignee(item)
        if not assignee_id:
            return ""
        try:
            member = await self.mm.get_channel_member_async(draft.channel_id, assignee_id)
        except Exception as error:
            log_event(
                log,
                "task_draft.assignee_skipped",
                level=30,
                status="unavailable",
                draft_id=draft.id,
                error_code=type(error).__name__,
            )
            return ""
        if str(member.get("user_id") or "") != assignee_id:
            log_event(
                log,
                "task_draft.assignee_skipped",
                level=30,
                status="rejected",
                draft_id=draft.id,
                error_code="MembershipMismatch",
            )
            return ""
        return assignee_id

    @staticmethod
    def _commit_message(task_count: int, assigned: int, scheduled: int) -> str:
        return (
            f"已创建 {task_count} 个正式任务；已确认负责人 {assigned} 个，"
            f"已确定截止时间 {scheduled} 个。歧义或缺失字段保持为空。"
        )

    def _actions(self, draft: TaskDraft) -> tuple[ResponseAction, ...]:
        if self.action_tokens is None or draft.state is not TaskDraftState.DRAFT:
            return ()
        shared = {
            "target": draft.id,
            "scope_id": draft.scope_id,
            "run_id": draft.run_id,
            "conversation_id": draft.channel_id,
            "root_id": draft.root_id,
            "requested_by": draft.requested_by,
        }
        try:
            commit = self.action_tokens.issue(action="task_draft_commit", **shared)
            reject = self.action_tokens.issue(action="task_draft_reject", **shared)
        except (TypeError, ValueError) as error:
            log.warning("任务草案 Action token 创建失败，将使用文本确认: %s", error)
            return ()
        return (
            ResponseAction(
                "task-draft-commit",
                "确认创建任务",
                "task_draft_commit",
                draft.id,
                style="success",
                fallback=f"`确认任务草案 {draft.id}`",
                token=commit,
            ),
            ResponseAction(
                "task-draft-reject",
                "放弃草案",
                "task_draft_reject",
                draft.id,
                style="danger",
                fallback=f"`放弃任务草案 {draft.id}`",
                token=reject,
            ),
        )

    def _audit(
        self,
        draft: TaskDraft,
        *,
        actor_id: str,
        decision: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.audit_store.append_audit(
            "task_draft.decision",
            actor_id=actor_id,
            scope_id=draft.scope_id,
            trace_id=draft.run_id,
            target=draft.id,
            decision=decision,
            details={"schema_version": "1.0", **(details or {})},
        )
