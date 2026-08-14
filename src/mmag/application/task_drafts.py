"""Meeting action-item draft workflow and explicit human confirmation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

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


class TaskDraftCoordinator:
    """Persist meeting action items and commit them through create_task."""

    def __init__(
        self,
        *,
        store: TaskDraftStore,
        memory: Memory,
        capability_registry,
        access_guard: MattermostAccessGuard,
        scope_resolver,
        action_tokens,
        audit_store,
    ) -> None:
        self.store = store
        self.memory = memory
        self.capability_registry = capability_registry
        self.access_guard = access_guard
        self.scope_resolver = scope_resolver
        self.action_tokens = action_tokens
        self.audit_store = audit_store

    def attach(self, post: dict, output: AgentOutput, view: ResponseView) -> ResponseView:
        result = output.result if isinstance(output.result, dict) else {}
        if (
            output.agent_name != "report"
            or "message_range" not in result
            or view.status is not RunStatus.SUCCEEDED
        ):
            return view
        scope = self.scope_resolver.resolve_post(post)
        items, rejected_count = self._items(result, post=post, scope=scope)
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
            details={"schema_version": "1.0", "item_count": len(draft.items)},
        )
        log_event(
            log,
            "task_draft.created",
            status="completed",
            draft_id=draft.id,
            item_count=len(draft.items),
            rejected_item_count=rejected_count,
        )
        commands = (
            f"确认创建：`确认任务草案 {draft.id}`",
            f"放弃草案：`放弃任务草案 {draft.id}`",
            "确认后会创建无负责人（assignee 为空）的正式任务；相关人物仅作为文本保留。",
        )
        warnings = view.warnings
        if rejected_count:
            warnings = (
                *warnings,
                f"有 {rejected_count} 个行动项缺少当前会话内可验证来源，未纳入草案。",
            )
        return replace(
            view,
            sections=(*view.sections, ResponseSection("任务草案", items=commands)),
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
        try:
            for index, item in enumerate(claimed.items):
                task = await self._create_task(claimed, item, index=index, scope=scope)
                task_ids.append(str(task["id"]))
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
        return committed, f"已创建 {len(task_ids)} 个正式任务，当前均未分配负责人。"

    def result_view(self, draft: TaskDraft, message: str) -> ResponseView:
        committed = draft.state is TaskDraftState.COMMITTED
        items = tuple(f"`{task_id}`" for task_id in draft.task_ids)
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
        due_text = str(item.get("due_text") or "未提及")
        source_ids = tuple(str(value) for value in item.get("source_post_ids") or ())
        description = (
            f"相关人物：{related_person}\n"
            f"截止时间原文：{due_text}\n"
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
                    "assignee_id": "",
                    "description": description,
                    "task_type": "meeting",
                    "due_time": 0,
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
        self, result: dict[str, Any], *, post: dict, scope: Scope
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
            due = str(raw.get("due_date") or "").strip()[:128]
            items.append(
                {
                    "content": content,
                    "related_person": related or "未提及",
                    "due_text": due or "未提及",
                    "source_post_ids": refs,
                }
            )
        return tuple(items), rejected

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
