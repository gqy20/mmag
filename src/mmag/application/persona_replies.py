"""Mattermost coordination for durable Digital Persona reply decisions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..config import config
from ..control_plane import PersonaReplyState, ScopeKind
from ..logger import get_logger, log_event
from .views import ResponseKind, ResponseView, RunStatus

if TYPE_CHECKING:
    from ..control_plane import MessagePipeline, PersonaReplyRequest, PersonaReplyStore
    from .actions import ActionClaims, ActionTokenService
    from .delivery import MattermostDelivery
    from .persona_ui import PersonaInvocation, PersonaWorkspaceUI

log = get_logger(__name__)
DraftGenerator = Callable[[dict, str], Awaitable[ResponseView]]


class PersonaReplyCoordinator:
    def __init__(
        self,
        *,
        mm_client,
        identity,
        scope_resolver,
        delivery: MattermostDelivery,
        workspace: PersonaWorkspaceUI,
        action_tokens: ActionTokenService | None,
        audit_store,
        draft_generator: DraftGenerator,
    ) -> None:
        self.mm = mm_client
        self.identity = identity
        self.scope_resolver = scope_resolver
        self.delivery = delivery
        self.workspace = workspace
        if workspace.reply_requests is None:
            raise ValueError("persona reply store is required")
        self.store: PersonaReplyStore = workspace.reply_requests
        self.action_tokens = action_tokens
        self.audit_store = audit_store
        self.draft_generator = draft_generator
        self.pipeline: MessagePipeline | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self, pipeline: MessagePipeline) -> None:
        self.pipeline = pipeline
        for request in self.store.list_states(PersonaReplyState.APPROVED):
            self._enqueue_decision(request, audit=False)
            self._audit(request, "persona.reply_recovered", "approved")
        await self._expire_due()
        self._expiry_task = asyncio.create_task(self._expiry_loop(), name="persona-reply-expiry")

    async def close(self) -> None:
        self._stopping = True
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            await asyncio.gather(self._expiry_task, return_exceptions=True)

    async def request_approval(
        self,
        post: dict,
        invocation: PersonaInvocation,
        *,
        status_post_id: str,
    ) -> None:
        draft = await self.draft_generator(post, status_post_id)
        if draft.status is not RunStatus.SUCCEEDED:
            await self.delivery.reply_view(post, draft, update_post_id=status_post_id)
            return
        requester_username = await self.mm.get_username_async(str(post.get("user_id") or ""))
        request = self.workspace.create_reply_request(
            invocation,
            post,
            requester_username=requester_username,
            draft_text=draft.summary,
            status_post_id=status_post_id,
        )
        try:
            channel = await self.mm.create_direct_channel_async(
                self.identity.user_id, invocation.represented_owner
            )
            owner_channel_id = str(channel["id"])
            request = self.store.set_approval_target(request.id, channel_id=owner_channel_id)
            owner_post = self._owner_post(request)
            await self.delivery.reply_view(
                owner_post,
                self.workspace.approval_view(request, owner_post),
                delivery_key=f"persona-reply:{request.id}:approval",
                agent_run_id=f"persona-reply:{request.id}",
                message_kind="persona_approval",
            )
        except Exception as error:
            self.store.mark_failed(request.id, type(error).__name__)
            log_event(
                log,
                "persona.reply_approval_failed",
                level=40,
                status="failed",
                error_code=type(error).__name__,
            )
            await self.delivery.reply_view(
                post,
                self._error(
                    "无法请求本人确认",
                    "回答草稿未发送，请稍后重试。",
                    request.id,
                ),
                update_post_id=status_post_id,
            )
            return
        self._audit(request, "persona.reply_requested", "pending")
        await self.delivery.reply_view(
            post,
            self.workspace.waiting_view(request),
            update_post_id=status_post_id,
            delivery_key=f"persona-reply:{request.id}:waiting",
            agent_run_id=f"persona-reply:{request.id}",
            message_kind="persona_waiting",
        )

    async def handle_action(self, payload: dict, claims: ActionClaims, actor_id: str) -> dict:
        if self.action_tokens is None or actor_id != claims.requested_by:
            raise PermissionError("persona reply action belongs to another owner")
        request = self.workspace.pending_reply(claims.target, actor_id=actor_id)
        token = str((payload.get("context") or {}).get("token") or "")
        if claims.action == "persona_reply_edit":
            self.action_tokens.consume(token, actor_id=actor_id)
            submit_token = self.action_tokens.issue(
                action="persona_reply_submit",
                target=request.id,
                scope_id=claims.scope_id,
                run_id=claims.run_id,
                conversation_id=claims.conversation_id,
                root_id=claims.root_id,
                requested_by=claims.requested_by,
            )
            await self.mm.open_dialog_async(
                trigger_id=str(payload.get("trigger_id") or ""),
                callback_url=config.mm_action_callback_url,
                dialog=self.workspace.reply_dialog(request, submit_token),
            )
            return {"ephemeral_text": "已打开回答编辑框。"}
        if claims.action not in {"persona_reply_approve", "persona_reply_reject"}:
            raise ValueError("unsupported persona reply action")
        self.action_tokens.consume(token, actor_id=actor_id)
        request = self.workspace.decide_reply(
            request.id,
            actor_id=actor_id,
            approved=claims.action == "persona_reply_approve",
        )
        self._enqueue_decision(request)
        message = (
            "已批准，正在发送。" if request.state is PersonaReplyState.APPROVED else "已拒绝发送。"
        )
        return {
            "update": {"message": message, "props": self._completed_props()},
            "ephemeral_text": message,
        }

    async def handle_dialog(self, payload: dict) -> dict:
        if self.action_tokens is None:
            raise RuntimeError("persona reply approval is not configured")
        token = str(payload.get("state") or "")
        actor_id = str(payload.get("user_id") or "")
        claims = self.action_tokens.verify(token)
        if claims.action != "persona_reply_submit" or actor_id != claims.requested_by:
            raise PermissionError("invalid persona reply dialog")
        channel_id = str(payload.get("channel_id") or "")
        if channel_id and channel_id != claims.conversation_id:
            raise PermissionError("persona reply dialog belongs to another conversation")
        submission = payload.get("submission")
        draft_text = (
            str(submission.get("draft") or "").strip() if isinstance(submission, dict) else ""
        )
        if not draft_text:
            return {"errors": {"draft": "回答内容不能为空"}}
        self.action_tokens.consume(token, actor_id=actor_id)
        request = self.workspace.decide_reply(
            claims.target, actor_id=actor_id, approved=True, draft_text=draft_text
        )
        self._enqueue_decision(request)
        self._enqueue_owner_update(request, "回答已修改，正在发送。")
        return {}

    async def handle_command(
        self, post: dict, command: tuple[str, str, str], *, status_post_id: str
    ) -> None:
        action, request_id, draft_text = command
        try:
            if action == "retry":
                request = self.store.retry_failed(
                    request_id, actor_id=str(post.get("user_id") or "")
                )
                self._retry_decision(request)
                summary = "失败的回答已进入重试队列。"
            else:
                request = self.workspace.decide_reply(
                    request_id,
                    actor_id=str(post.get("user_id") or ""),
                    approved=action == "approve",
                    draft_text=draft_text,
                )
                self._enqueue_decision(request)
                summary = "回答正在发送。" if action == "approve" else "已拒绝发送。"
            view = ResponseView(
                kind=ResponseKind.STATUS,
                title="代答处理完成",
                summary=summary,
                status=RunStatus.SUCCEEDED,
            )
        except (KeyError, PermissionError, ValueError) as error:
            log_event(
                log,
                "persona.reply_decision_failed",
                level=30,
                status="failed",
                error_code=type(error).__name__,
            )
            view = self._error(
                "代答处理失败",
                "请求不存在、已处理、已过期，或不属于当前用户。",
                request_id,
            )
        await self.delivery.reply_view(post, view, update_post_id=status_post_id)

    def _enqueue_decision(self, request: PersonaReplyRequest, *, audit: bool = True) -> None:
        pipeline = self._require_pipeline()
        post = self._source_post(request)
        view = (
            self.workspace.final_view(request)
            if request.state is PersonaReplyState.APPROVED
            else self.workspace.rejected_view(request)
        )
        for message in self.delivery.render_messages(
            post,
            view,
            update_post_id=request.source_status_post_id,
            delivery_key=f"persona-reply:{request.id}:decision",
            agent_run_id=f"persona-reply:{request.id}",
            message_kind="persona_decision",
        ):
            pipeline.enqueue_delivery(message)
        if audit:
            self._audit(request, "persona.reply_decided", request.state.value)

    def _retry_decision(self, request: PersonaReplyRequest) -> None:
        pipeline = self._require_pipeline()
        messages = self.delivery.render_messages(
            self._source_post(request),
            self.workspace.final_view(request),
            update_post_id=request.source_status_post_id,
            delivery_key=f"persona-reply:{request.id}:decision",
            agent_run_id=f"persona-reply:{request.id}",
            message_kind="persona_decision",
        )
        for message in messages:
            if not pipeline.retry_delivery(message.idempotency_key):
                pipeline.enqueue_delivery(message)
        self._audit(request, "persona.reply_retried", "retrying")

    async def _expiry_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(5)
                await self._expire_due()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("persona reply expiry reconciliation failed")

    async def _expire_due(self) -> None:
        for request in self.store.expire_pending():
            pipeline = self._require_pipeline()
            for message in self.delivery.render_messages(
                self._source_post(request),
                self.workspace.expired_view(request),
                update_post_id=request.source_status_post_id,
                delivery_key=f"persona-reply:{request.id}:expired",
                agent_run_id=f"persona-reply:{request.id}",
                message_kind="persona_expired",
            ):
                pipeline.enqueue_delivery(message)
            if request.owner_channel_id and request.owner_approval_post_id:
                self._enqueue_owner_update(request, "该代答请求已超时关闭。")
            self._audit(request, "persona.reply_expired", "expired")

    def _enqueue_owner_update(self, request: PersonaReplyRequest, text: str) -> None:
        if not request.owner_channel_id or not request.owner_approval_post_id:
            return
        post = self._owner_post(request)
        view = ResponseView(
            kind=ResponseKind.STATUS,
            title="数字人代答",
            summary=text,
            status=RunStatus.SUCCEEDED,
        )
        for message in self.delivery.render_messages(
            post,
            view,
            update_post_id=request.owner_approval_post_id,
            delivery_key=f"persona-reply:{request.id}:owner-update:{request.state.value}",
            agent_run_id=f"persona-reply:{request.id}",
            message_kind="persona_owner_update",
        ):
            self._require_pipeline().enqueue_delivery(message)

    def _owner_post(self, request: PersonaReplyRequest) -> dict:
        return {
            "id": "",
            "channel_id": request.owner_channel_id,
            "user_id": request.owner_id,
            "_scope_id": self.scope_resolver.scope_id(
                request.installation_id,
                request.tenant_id,
                ScopeKind.PERSONAL,
                request.owner_id,
            ),
        }

    @staticmethod
    def _source_post(request: PersonaReplyRequest) -> dict:
        return {
            "id": request.source_root_id,
            "root_id": request.source_root_id,
            "channel_id": request.source_channel_id,
            "user_id": request.requester_id,
            "_scope_id": request.source_scope_id,
        }

    def _audit(self, request: PersonaReplyRequest, event: str, decision: str) -> None:
        self.audit_store.append_audit(
            event,
            actor_id=request.decision_by or request.requester_id,
            scope_id=request.source_scope_id,
            trace_id=f"persona-reply:{request.id}",
            target=request.id,
            decision=decision,
            details={
                "schema_version": "1.0",
                "persona_ref": request.persona_ref,
                "persona_hash": request.persona_hash,
                "owner_id": request.owner_id,
                "requester_id": request.requester_id,
            },
        )

    def _require_pipeline(self) -> MessagePipeline:
        if self.pipeline is None:
            raise RuntimeError("persona reply delivery pipeline is not attached")
        return self.pipeline

    @staticmethod
    def _completed_props() -> dict[str, str]:
        return {
            "from_bot": "true",
            "mmag_kind": "persona_approval",
            "mmag_status": "completed",
        }

    @staticmethod
    def _error(title: str, summary: str, request_id: str) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.ERROR,
            title=title,
            summary=summary,
            status=RunStatus.FAILED,
            run_id=f"persona-reply:{request_id}",
        )
