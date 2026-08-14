"""Inbound Mattermost message orchestration through the managed-Agent router."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from ..agent_system import AgentOutput
from ..config import config
from ..control_plane import (
    InboundEvent,
    MattermostScopeResolver,
    MessagePipeline,
    OutboundMessage,
    ScopeKind,
)
from ..logger import get_logger, log_context, log_event
from ..runtimes import (
    RuntimeStatus,
)
from .action_responses import load_action_post, preserve_action_post
from .agent_requests import AgentRequestHandler
from .delivery import OUTBOUND_COLLECTOR, MattermostDelivery
from .persona_replies import PersonaReplyCoordinator
from .persona_ui import PersonaWorkspaceUI
from .personal_ui import PersonalWorkspaceUI
from .task_drafts import TaskDraftCoordinator

if TYPE_CHECKING:
    from ..skill_packages import SkillResolver
    from .context import AttachmentProcessor, BotIdentity, ContextBuilder

log = get_logger(__name__)


class MessageHandler:
    """Own one inbound message from normalization through routed execution."""

    def __init__(
        self,
        *,
        mm_client,
        memory,
        compactor,
        capability_registry,
        agent_router,
        skill_resolver: SkillResolver,
        audit_store,
        approval_coordinator,
        working_memory: dict[str, list],
        identity: BotIdentity,
        attachment_processor: AttachmentProcessor,
        context_builder: ContextBuilder,
        delivery: MattermostDelivery,
        stats: dict[str, int],
        action_tokens=None,
        scope_resolver: MattermostScopeResolver | None = None,
        personal_skills=None,
        work_cases=None,
        interactions=None,
        intent_runtime=None,
        memory_items=None,
        personas=None,
        persona_replies=None,
        task_drafts=None,
        access_guard=None,
    ) -> None:
        self.mm = mm_client
        self.memory = memory
        self.compactor = compactor
        self.capability_registry = capability_registry
        self.agent_router = agent_router
        self.skill_resolver = skill_resolver
        self.audit_store = audit_store
        self.approval_coordinator = approval_coordinator
        self.working_memory = working_memory
        self.identity = identity
        self.attachments = attachment_processor
        self.context_builder = context_builder
        self.delivery = delivery
        self.stats = stats
        self.action_tokens = action_tokens
        self.scope_resolver = scope_resolver or MattermostScopeResolver(
            mm_client,
            installation_id=config.mm_installation_id,
            tenant_id=config.mm_tenant_id,
        )
        self.personal_ui = (
            PersonalWorkspaceUI(
                personal_skills=personal_skills,
                work_cases=work_cases,
                interactions=interactions,
                action_tokens=action_tokens,
                audit_store=audit_store,
                intent_runtime=intent_runtime,
                memories=memory_items,
                personas=personas,
            )
            if personal_skills is not None and work_cases is not None and interactions is not None
            else None
        )
        self.persona_ui = (
            PersonaWorkspaceUI(
                personas=personas,
                memories=memory_items,
                action_tokens=action_tokens,
                reply_requests=persona_replies,
                audit_store=audit_store,
            )
            if personas is not None and memory_items is not None
            else None
        )
        self.task_drafts = (
            TaskDraftCoordinator(
                store=task_drafts,
                memory=memory,
                capability_registry=capability_registry,
                access_guard=access_guard,
                scope_resolver=self.scope_resolver,
                action_tokens=action_tokens,
                audit_store=audit_store,
            )
            if task_drafts is not None and access_guard is not None
            else None
        )
        self.agent_requests = AgentRequestHandler(
            capability_registry=capability_registry,
            agent_router=agent_router,
            skill_resolver=skill_resolver,
            audit_store=audit_store,
            approval_coordinator=approval_coordinator,
            context_builder=context_builder,
            delivery=delivery,
            scope_resolver=self.scope_resolver,
            personal_ui=self.personal_ui,
            action_tokens=action_tokens,
            task_drafts=self.task_drafts,
        )
        self.persona_replies = (
            PersonaReplyCoordinator(
                mm_client=mm_client,
                identity=identity,
                scope_resolver=self.scope_resolver,
                delivery=delivery,
                workspace=self.persona_ui,
                action_tokens=action_tokens,
                audit_store=audit_store,
                draft_generator=self.agent_requests.respond_draft,
            )
            if self.persona_ui is not None and persona_replies is not None
            else None
        )
        self._action_tasks: set[asyncio.Task] = set()
        self.presenter = self.agent_requests.presenter
        self.pipeline: MessagePipeline | None = None
        self._ingress_status_posts: dict[str, str] = {}

    async def on_posted(self, event: dict) -> None:
        if self.pipeline is None:
            raise RuntimeError("Mattermost Inbox Pipeline is not attached")
        inbound = self.to_inbound_event(event)
        if inbound is not None:
            await self.pipeline.accept(inbound, on_accepted=self._acknowledge_accepted)

    def on_post_edited(self, event: dict) -> None:
        post = self._parse_event_post(event)
        if post is None or not self.memory.update_message(post):
            return
        self._revoke_message_memories(str(post.get("id") or ""))
        channel_id = str(post.get("channel_id") or "")
        for cached in self.working_memory.get(channel_id, []):
            if cached.get("id") == post.get("id"):
                cached["message"] = post.get("message", "")
                cached["edit_at"] = post.get("edit_at", 0)
                break
        log_event(log, "message.edited", status="completed", message_id=post["id"])

    def on_post_deleted(self, event: dict) -> None:
        post = self._parse_event_post(event)
        if post is None:
            return
        post_id = str(post.get("id") or "")
        if not post_id or not self.memory.delete_message(post_id):
            return
        self._revoke_message_memories(post_id)
        channel_id = str(post.get("channel_id") or "")
        if channel_id in self.working_memory:
            self.working_memory[channel_id] = [
                cached for cached in self.working_memory[channel_id] if cached.get("id") != post_id
            ]
        log_event(log, "message.deleted", status="completed", message_id=post_id)

    def _revoke_message_memories(self, post_id: str) -> None:
        if not post_id or self.personal_ui is None or self.personal_ui.memories is None:
            return
        memory_ids = self.personal_ui.memories.ids_for_source(
            "mattermost_post",
            post_id,
            installation_id=self.scope_resolver.installation_id,
            tenant_id=self.scope_resolver.tenant_id,
        )
        revoked = self.personal_ui.memories.revoke_source(
            "mattermost_post",
            post_id,
            installation_id=self.scope_resolver.installation_id,
            tenant_id=self.scope_resolver.tenant_id,
        )
        if self.persona_ui is not None:
            for memory_id in memory_ids:
                self.persona_ui.personas.archive_by_memory(memory_id)
        if revoked:
            log_event(
                log,
                "memory.source_revoked",
                status="completed",
                source_type="mattermost_post",
                revoked_count=revoked,
            )

    async def _acknowledge_accepted(self, event: InboundEvent) -> None:
        post = self._parse_post(dict(event.payload))
        if post is None or not self._accept(post):
            return
        if post.get("_mmag_entry") == "slash":
            return
        message = str(post.get("message") or "").strip()
        file_metas = (post.get("metadata") or {}).get("files") or []
        if not message and not file_metas:
            return
        status_post_id = await self.delivery.send_ack(post) or ""
        if status_post_id:
            self._ingress_status_posts[event.event_id] = status_post_id

    @staticmethod
    def to_inbound_event(event: dict) -> InboundEvent | None:
        raw = event.get("data", {}).get("post")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw, dict):
            return None
        event_id = str(raw.get("id") or "")
        conversation_id = str(raw.get("channel_id") or "")
        if not event_id or not conversation_id:
            return None
        return InboundEvent(
            event_id=event_id,
            platform="mattermost",
            event_type="posted",
            conversation_id=conversation_id,
            actor_id=str(raw.get("user_id") or ""),
            occurred_at=float(raw.get("create_at") or time.time() * 1000) / 1000,
            payload=event,
        )

    async def process_inbound(self, event: InboundEvent) -> tuple[OutboundMessage, ...]:
        collector: list[OutboundMessage] = []
        token = OUTBOUND_COLLECTOR.set(collector)
        try:
            await self.process_posted_event(dict(event.payload))
            messages = tuple(collector)
            log_event(
                log,
                "inbox.processor_completed",
                status="completed",
                outbound_count=len(messages),
            )
            return messages
        finally:
            OUTBOUND_COLLECTOR.reset(token)

    async def process_posted_event(self, event: dict) -> None:
        post = self._parse_post(event)
        if post is None or not self._accept(post):
            return
        channel_id = str(post["channel_id"])
        message = str(post.get("message") or "").strip()
        file_metas = (post.get("metadata") or {}).get("files") or []
        if not message and not file_metas:
            return
        post_id = str(post.get("id") or "")
        with log_context.bind(
            trace_id=log_context.new_trace_id(),
            conversation_id=channel_id,
            actor_id=str(post.get("user_id") or ""),
            run_id=self._run_id(post),
        ):
            await self._process_accepted_post(post, message, file_metas, post_id)

    async def _process_accepted_post(
        self,
        post: dict,
        message: str,
        file_metas: list,
        post_id: str,
    ) -> None:
        channel_id = str(post["channel_id"])

        is_slash = post.get("_mmag_entry") == "slash"
        status_post_id = self._ingress_status_posts.pop(post_id, "")
        if not status_post_id and not is_slash:
            status_post_id = await self.delivery.send_ack(post) or ""
        self.stats["messages"] += 1
        user_id = str(post.get("user_id") or "")
        access_scope = self.scope_resolver.resolve_post(post)
        self.audit_store.put_scope(access_scope)
        post["_scope_id"] = access_scope.id
        post["username"] = self.mm.get_username(user_id)
        post["_llm_content_blocks"] = await self.attachments.build_blocks(
            file_metas,
            max_count=config.max_images_per_msg,
            max_bytes=config.max_image_bytes,
            max_text_chars=config.max_text_attachment_chars,
        )
        if not is_slash:
            if not self.memory.log_message(post):
                self.stats["dropped_messages"] += 1
            await self.compactor.maybe_compact(channel_id)
            window = self.working_memory.setdefault(channel_id, [])
            window.append(post)
            del window[: -config.max_context_messages]
            if access_scope.kind is ScopeKind.PERSONAL:
                self.memory.update_profile_from_message(user_id, post["username"], post)
        log_event(log, "message.accepted", status="accepted", attachment_count=len(file_metas))
        if self.personal_ui is not None:
            handled, view, personal_ref = await self.personal_ui.consume_message(
                post, message, access_scope
            )
            if personal_ref:
                post["_requested_personal_skill"] = personal_ref
            if handled:
                if view is not None:
                    await self.delivery.reply_view(post, view, update_post_id=status_post_id)
                return
        if self.persona_ui is not None:
            reply_command = self.persona_ui.reply_command(message)
            if reply_command is not None and access_scope.kind is ScopeKind.PERSONAL:
                if self.persona_replies is None:
                    raise RuntimeError("persona reply approval is not configured")
                await self.persona_replies.handle_command(
                    post,
                    reply_command,
                    status_post_id=status_post_id,
                )
                return
            handled, view = self.persona_ui.consume_owner(post, message, access_scope)
            if handled:
                if view is not None:
                    await self.delivery.reply_view(post, view, update_post_id=status_post_id)
                return
            invocation, rejected = self.persona_ui.resolve_question(
                message,
                installation_id=access_scope.installation_id,
                tenant_id=access_scope.tenant_id,
            )
            if rejected is not None:
                self.audit_store.append_audit(
                    "persona.query",
                    actor_id=user_id,
                    scope_id=access_scope.id,
                    trace_id=f"mattermost:{post_id}",
                    target="",
                    decision="rejected",
                    details={
                        "schema_version": "1.0",
                        "reason": rejected.title,
                    },
                )
                await self.delivery.reply_view(post, rejected, update_post_id=status_post_id)
                return
            if invocation is not None:
                post["_persona_ref"] = invocation.persona_ref
                post["_persona_question"] = invocation.question
                post["_persona_display_name"] = invocation.display_name
                post["_represented_owner"] = invocation.represented_owner
                post["_persona_hash"] = invocation.persona_hash
                self.audit_store.append_audit(
                    "persona.query",
                    actor_id=user_id,
                    scope_id=access_scope.id,
                    trace_id=f"mattermost:{post_id}",
                    target=invocation.persona_ref,
                    decision="accepted",
                    details={
                        "schema_version": "1.0",
                        "represented_owner": invocation.represented_owner,
                        "persona_hash": invocation.persona_hash,
                    },
                )
                typing_task = asyncio.create_task(self.delivery.typing_loop(channel_id))
                try:
                    if invocation.approval_required:
                        if self.persona_replies is None:
                            raise RuntimeError("persona reply approval is not configured")
                        await self.persona_replies.request_approval(
                            post, invocation, status_post_id=status_post_id
                        )
                    else:
                        await self.agent_requests.respond(
                            post, tag="persona", status_post_id=status_post_id
                        )
                finally:
                    typing_task.cancel()
                return
        approval = self.approval_command(message, bot_username=self.identity.username)
        task_draft_command = (
            self.task_drafts.parse_command(message, bot_username=self.identity.username)
            if self.task_drafts is not None
            else None
        )
        if task_draft_command is not None:
            await self._handle_task_draft_command(
                post,
                task_draft_command,
                scope=access_scope,
                status_post_id=status_post_id,
            )
            return
        if approval is not None:
            with log_context.bind(operation="approval"):
                await self._handle_approval_command(post, approval)
            return

        if self.is_explicit_invocation(post):
            typing_task = asyncio.create_task(self.delivery.typing_loop(channel_id))
            try:
                await self.agent_requests.respond(
                    post, tag="mention", status_post_id=status_post_id
                )
            finally:
                typing_task.cancel()
            return

        with log_context.bind(operation="decide"):
            response = await self.agent_requests.decide_and_respond(post)
        if self.agent_requests.is_silent(response):
            log_event(log, "agent.silent", status="completed")
            return
        await self.delivery.reply(post, response)

    @staticmethod
    def _parse_post(event: dict) -> dict | None:
        parsed = MessageHandler._parse_event_post(event)
        return parsed if parsed is not None and "message" in parsed else None

    @staticmethod
    def _parse_event_post(event: dict) -> dict | None:
        raw = event.get("data", {}).get("post")
        if isinstance(raw, dict):
            parsed = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as error:
                log.warning("无法解析 post JSON: %s", error)
                return None
        else:
            return None
        return parsed if isinstance(parsed, dict) and "id" in parsed else None

    def _accept(self, post: dict) -> bool:
        if post.get("user_id") == self.identity.user_id or post.get("type"):
            return False
        props = post.get("props")
        if isinstance(props, dict) and str(props.get("from_bot") or "").lower() == "true":
            return False
        message = str(post.get("message") or "").strip()
        first_word = message.split(maxsplit=1)[0].lower() if message else ""
        own_mention = f"@{self.identity.username.lower()}"
        if (
            first_word.startswith("@")
            and first_word != own_mention
            and own_mention not in message.lower()
        ):
            return False
        approval = self.approval_command(message, bot_username=self.identity.username)
        if approval is not None:
            store = getattr(self.approval_coordinator, "store", None)
            if store is not None:
                try:
                    store.get_approval_request(approval[1])
                except KeyError:
                    log_event(
                        log,
                        "approval.command.ignored",
                        status="ignored",
                        reason="not_owned",
                    )
                    return False
        channel_id = str(post.get("channel_id") or "")
        if config.mm_channel_id and channel_id != config.mm_channel_id:
            return False
        if config.mm_team_id:
            channel = self.mm.get_channel(channel_id)
            if channel.get("type") in {"D", "G"}:
                return True
            return channel.get("team_id", "") == config.mm_team_id
        return True

    def is_explicit_invocation(self, post: dict) -> bool:
        if post.get("_mmag_entry") == "slash":
            return True
        message = str(post.get("message") or "").lower()
        if f"@{self.identity.username.lower()}" in message:
            return True
        if self.mm.get_channel(post["channel_id"]).get("type") == "D":
            return True
        root_id = str(post.get("root_id") or "")
        return bool(root_id and self.memory.get_post_user(root_id) == self.identity.user_id)

    async def handle_action_callback(self, payload: dict) -> dict:
        if self.action_tokens is None:
            raise PermissionError("interactive actions are not configured")
        if str(payload.get("type") or "") == "dialog_submission":
            return await self._handle_dialog_submission(payload)
        context = payload.get("context")
        token = context.get("token") if isinstance(context, dict) else ""
        actor_id = str(payload.get("user_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        claims = self.action_tokens.verify(str(token or ""))
        log_event(
            log,
            "mattermost.action_received",
            status="accepted",
            action=claims.action,
            action_jti=claims.jti,
        )
        if channel_id != claims.conversation_id:
            raise PermissionError("action belongs to another conversation")
        scope_id = self.post_scope({"channel_id": channel_id, "user_id": actor_id})
        if scope_id != claims.scope_id:
            raise PermissionError("action belongs to another scope")
        if claims.action.startswith("persona_reply_"):
            if self.persona_replies is None:
                raise RuntimeError("persona reply approval is not configured")
            return await self.persona_replies.handle_action(payload, claims, actor_id)
        if claims.action.startswith("task_draft_"):
            if self.task_drafts is None:
                raise RuntimeError("task draft workflow is not configured")
            action_post = await load_action_post(
                self.mm,
                payload,
                channel_id=channel_id,
                bot_user_id=self.identity.user_id,
            )
            claims = self.action_tokens.consume(str(token), actor_id=actor_id)
            scope = self.scope_resolver.resolve_post(
                {"channel_id": channel_id, "user_id": actor_id}
            )
            draft, message = await self.task_drafts.decide(
                claims.target,
                actor_id=actor_id,
                scope=scope,
                approved=claims.action == "task_draft_commit",
            )
            log_event(
                log,
                "mattermost.action_completed",
                status="completed",
                action=claims.action,
                action_jti=claims.jti,
            )
            return preserve_action_post(
                action_post,
                payload,
                message,
                terminal=True,
                status="succeeded" if draft.task_ids else "rejected",
            )
        if claims.action == "persona_policy_edit":
            return await self._open_persona_policy_dialog(payload, claims, actor_id)
        if claims.action.startswith("persona_"):
            if self.persona_ui is None:
                raise RuntimeError("persona workspace is not configured")
            action_post = await load_action_post(
                self.mm,
                payload,
                channel_id=channel_id,
                bot_user_id=self.identity.user_id,
            )
            claims = self.action_tokens.consume(str(token), actor_id=actor_id)
            scope = self.scope_resolver.resolve_post(
                {"channel_id": channel_id, "user_id": actor_id}
            )
            message = self.persona_ui.handle_action(
                claims,
                actor_id=actor_id,
                post={"channel_id": channel_id, "user_id": actor_id},
                scope=scope,
            )
            log_event(
                log,
                "mattermost.action_completed",
                status="completed",
                action=claims.action,
                action_jti=claims.jti,
            )
            return preserve_action_post(action_post, payload, message, terminal=True)
        if claims.action.startswith(("pskill_", "case_", "memory_")):
            if self.personal_ui is None:
                raise RuntimeError("personal workspace is not configured")
            action_post = await load_action_post(
                self.mm,
                payload,
                channel_id=channel_id,
                bot_user_id=self.identity.user_id,
            )
            claims = self.action_tokens.consume(str(token), actor_id=actor_id)
            scope = self.scope_resolver.resolve_post(
                {"channel_id": channel_id, "user_id": actor_id}
            )
            message = self.personal_ui.handle_action(
                claims,
                actor_id=actor_id,
                post={"channel_id": channel_id, "user_id": actor_id},
                scope=scope,
            )
            log_event(
                log,
                "mattermost.action_completed",
                status="completed",
                action=claims.action,
                action_jti=claims.jti,
            )
            terminal = claims.action in {"pskill_activate", "pskill_archive", "memory_forget"}
            return preserve_action_post(
                action_post,
                payload,
                message,
                terminal=terminal,
            )
        if claims.action not in {"approve", "reject"}:
            raise ValueError("action is not supported by this callback")
        approval = self.approval_coordinator.store.get_approval_request(claims.target)
        if not await self.approval_coordinator.authorizer.can_decide(approval, actor_id):
            raise PermissionError("actor is not authorized to decide this approval")
        action_post = await load_action_post(
            self.mm,
            payload,
            channel_id=channel_id,
            bot_user_id=self.identity.user_id,
        )
        claims = self.action_tokens.consume(str(token), actor_id=actor_id)
        task = asyncio.create_task(
            self._complete_action(claims, actor_id),
            name=f"action:{claims.jti}",
        )
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)
        decision = "已批准，正在继续执行。" if claims.action == "approve" else "已拒绝。"
        log_event(
            log,
            "mattermost.action_completed",
            status="completed",
            action=claims.action,
            action_jti=claims.jti,
        )
        return preserve_action_post(
            action_post,
            payload,
            decision,
            terminal=True,
            status="running" if claims.action == "approve" else "failed",
        )

    async def _handle_dialog_submission(self, payload: dict) -> dict:
        if self.action_tokens is None:
            raise PermissionError("interactive actions are not configured")
        claims = self.action_tokens.verify(str(payload.get("state") or ""))
        if claims.action == "persona_reply_submit":
            if self.persona_replies is None:
                raise RuntimeError("persona reply approval is not configured")
            return await self.persona_replies.handle_dialog(payload)
        if claims.action != "persona_policy_submit":
            raise ValueError("unsupported dialog submission")
        return await self._submit_persona_policy(payload, claims)

    async def _open_persona_policy_dialog(self, payload: dict, claims, actor_id: str) -> dict:
        if self.persona_ui is None or self.action_tokens is None:
            raise RuntimeError("persona workspace is not configured")
        if actor_id != claims.requested_by:
            raise PermissionError("persona policy belongs to another owner")
        token = str((payload.get("context") or {}).get("token") or "")
        self.persona_ui.personas.get(claims.target, owner_id=actor_id)
        self.action_tokens.consume(token, actor_id=actor_id)
        submit = self.action_tokens.issue(
            action="persona_policy_submit",
            target=claims.target,
            scope_id=claims.scope_id,
            run_id=claims.run_id,
            conversation_id=claims.conversation_id,
            root_id=claims.root_id,
            requested_by=claims.requested_by,
        )
        await self.mm.open_dialog_async(
            trigger_id=str(payload.get("trigger_id") or ""),
            callback_url=config.mm_action_callback_url,
            dialog=self.persona_ui.policy_dialog(claims.target, actor_id=actor_id, state=submit),
        )
        return {"ephemeral_text": "已打开数字人策略编辑框。"}

    async def _submit_persona_policy(self, payload: dict, claims) -> dict:
        if self.persona_ui is None or self.action_tokens is None:
            raise RuntimeError("persona workspace is not configured")
        actor_id = str(payload.get("user_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        if actor_id != claims.requested_by or channel_id != claims.conversation_id:
            raise PermissionError("persona policy dialog belongs to another owner")
        scope = self.scope_resolver.resolve_post({"channel_id": channel_id, "user_id": actor_id})
        if scope.id != claims.scope_id:
            raise PermissionError("persona policy dialog belongs to another scope")
        submission = payload.get("submission")
        if not isinstance(submission, dict):
            return {"errors": {"mode": "策略内容不能为空"}}
        try:
            self.persona_ui.validate_policy(submission)
        except ValueError as error:
            return {"errors": {"mode": str(error)}}
        self.action_tokens.consume(str(payload.get("state") or ""), actor_id=actor_id)
        revised = self.persona_ui.revise_policy(claims.target, actor_id=actor_id, values=submission)
        post = {
            "id": claims.root_id,
            "root_id": claims.root_id,
            "channel_id": channel_id,
            "user_id": actor_id,
            "_scope_id": scope.id,
        }
        self.audit_store.append_audit(
            "persona.revision",
            actor_id=actor_id,
            scope_id=scope.id,
            trace_id=claims.run_id,
            target=revised.ref,
            decision="policy_revised",
            details={"schema_version": "1.0", "previous_ref": claims.target},
        )
        await self.delivery.reply_view(post, self.persona_ui.view(post, scope))
        return {}

    async def close_actions(self) -> None:
        tasks = tuple(self._action_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=30)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _complete_action(self, claims, actor_id: str) -> None:
        post = {
            "id": claims.root_id,
            "root_id": claims.root_id,
            "channel_id": claims.conversation_id,
            "user_id": claims.requested_by,
            "message": "approval resume",
        }
        try:
            result = await self.approval_coordinator.resume(
                claims.target,
                approved=claims.action == "approve",
                actor_id=actor_id,
                scope_id=claims.scope_id,
                trace_id=claims.run_id,
                reason="Mattermost interactive action",
            )
            view = (
                self.agent_requests.register_approval_interrupt(post, result)
                if result.status is RuntimeStatus.WAITING_APPROVAL
                else self.presenter.present(
                    AgentOutput(
                        text=result.text,
                        agent_name="approval",
                        artifacts=tuple(dict(item) for item in result.artifacts),
                        result=(dict(result.output) if result.output is not None else None),
                        runtime_result=result,
                    ),
                    run_id=claims.run_id,
                )
            )
        except Exception as error:
            log.error("交互审批恢复失败: %s", error, exc_info=True)
            view = self.presenter.error(
                title="审批恢复失败",
                summary="审批已记录，但任务恢复失败，请联系运维并提供 Run ID。",
                run_id=claims.run_id,
            )
        await self.delivery.reply_view(
            post,
            view,
            delivery_key=f"{claims.run_id}:action:{claims.jti}",
        )

    async def _handle_task_draft_command(
        self,
        post: dict,
        command: tuple[bool, str],
        *,
        scope,
        status_post_id: str,
    ) -> None:
        if self.task_drafts is None:
            return
        approved, draft_id = command
        try:
            draft, message = await self.task_drafts.decide(
                draft_id,
                actor_id=str(post.get("user_id") or ""),
                scope=scope,
                approved=approved,
            )
            view = self.task_drafts.result_view(draft, message)
        except (KeyError, PermissionError, RuntimeError, ValueError) as error:
            log_event(
                log,
                "task_draft.command_failed",
                status="failed",
                error_code=type(error).__name__,
            )
            log.warning("任务草案命令处理失败: %s", error)
            view = self.presenter.error(
                title="任务草案处理失败",
                summary="无法处理该草案，请确认草案 ID、当前会话、创建者和草案状态。",
                run_id=self._run_id(post),
            )
        log_event(
            log,
            "task_draft.response_ready",
            status="ready",
            draft_id=draft_id,
            approved=approved,
        )
        await self.delivery.reply_view(
            post,
            view,
            update_post_id=status_post_id,
            delivery_key=self._run_id(post),
            agent_run_id=f"run:{post['id']}",
        )
        log_event(
            log,
            "task_draft.response_collected",
            status="completed",
            draft_id=draft_id,
        )

    def post_scope(self, post: dict) -> str:
        return self.scope_resolver.resolve_post(post).id

    @staticmethod
    def _run_id(post: dict) -> str:
        post_id = str(post.get("id") or log_context.get("trace_id", "unknown"))
        return f"mattermost:{post_id}"

    async def _handle_approval_command(self, post: dict, command: tuple[bool, str]) -> None:
        approved, request_id = command
        try:
            result = await self.approval_coordinator.resume(
                request_id,
                approved=approved,
                actor_id=post.get("user_id", ""),
                scope_id=self.post_scope(post),
                trace_id=log_context.get("trace_id", "----"),
                reason="",
            )
            response_view = (
                self.agent_requests.register_approval_interrupt(post, result)
                if result.status is RuntimeStatus.WAITING_APPROVAL
                else self.presenter.present(
                    AgentOutput(
                        text=result.text,
                        agent_name="approval",
                        artifacts=tuple(dict(item) for item in result.artifacts),
                        result=(dict(result.output) if result.output is not None else None),
                        runtime_result=result,
                    ),
                    run_id=self._run_id(post),
                )
            )
        except KeyError:
            log_event(
                log,
                "approval.command.ignored",
                status="ignored",
                reason="not_owned",
            )
            return
        except (PermissionError, ValueError) as error:
            log_event(
                log,
                "approval.command.failed",
                status="failed",
                error_code=type(error).__name__,
            )
            response_view = self.presenter.error(
                title="审批失败",
                summary="当前审批无法处理，请确认审批 ID、权限和状态。",
                run_id=self._run_id(post),
            )
            log.warning("审批处理失败: %s", error)
        await self.delivery.reply_view(post, response_view)

    @staticmethod
    def approval_command(
        message: str, *, bot_username: str = ""
    ) -> tuple[bool, str] | None:
        parts = message.strip().split()
        if (
            len(parts) == 3
            and bot_username
            and parts[0].lower() == f"@{bot_username.lower()}"
        ):
            parts = parts[1:]
        if len(parts) != 2:
            return None
        if parts[0].lower() in {"批准", "approve"}:
            return True, parts[1]
        if parts[0].lower() in {"拒绝", "reject"}:
            return False, parts[1]
        return None
