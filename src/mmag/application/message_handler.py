"""Inbound Mattermost message orchestration through the managed-Agent router."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..agent_packages import AgentPackageError
from ..agent_system import AgentOutput, AgentRequest
from ..capabilities import CapabilityContext, bind_capability_context
from ..config import config
from ..control_plane import InboundEvent, MessagePipeline, OutboundMessage
from ..governance import GovernanceContext, bind_governance_context
from ..logger import get_logger, log_context, log_event
from ..runtimes import (
    AgentResult,
    AgentRuntimeError,
    RunContext,
    RunEventSink,
    RunRequest,
    RuntimeRateLimitError,
    RuntimeRejectedError,
    RuntimeStatus,
    RuntimeTimeoutError,
    RuntimeUnavailableError,
)
from ..skill_packages import SkillPackageError
from .delivery import OUTBOUND_COLLECTOR, MattermostDelivery
from .views import ResponseAction, ResponsePresenter, ResponseView

if TYPE_CHECKING:
    from ..agent_system import ManagedAgent
    from ..skill_packages import SkillResolver
    from .context import AttachmentProcessor, BotIdentity, ContextBuilder
    from .stream import MattermostStream

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
        self._action_tasks: set[asyncio.Task] = set()
        self.presenter = ResponsePresenter()
        self.pipeline: MessagePipeline | None = None
        self._ingress_status_posts: dict[str, str] = {}

    async def on_posted(self, event: dict) -> None:
        if self.pipeline is None:
            await self.process_posted_event(event)
            return
        inbound = self.to_inbound_event(event)
        if inbound is not None:
            await self.pipeline.accept(inbound, on_accepted=self._acknowledge_accepted)

    async def _acknowledge_accepted(self, event: InboundEvent) -> None:
        post = self._parse_post(dict(event.payload))
        if post is None or not self._accept(post):
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
            return tuple(collector)
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
        if self.pipeline is None and post_id and self.memory.has_message(post_id):
            log_event(log, "message.duplicate", status="skipped")
            return

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

        status_post_id = self._ingress_status_posts.pop(post_id, "")
        if not status_post_id:
            status_post_id = await self.delivery.send_ack(post) or ""
        self.stats["messages"] += 1
        user_id = str(post.get("user_id") or "")
        post["username"] = self.mm.get_username(user_id)
        post["_llm_content_blocks"] = await self.attachments.build_blocks(
            file_metas,
            max_count=config.max_images_per_msg,
            max_bytes=config.max_image_bytes,
            max_text_chars=config.max_text_attachment_chars,
        )
        if not self.memory.log_message(post):
            self.stats["dropped_messages"] += 1
        await self.compactor.maybe_compact(channel_id)
        window = self.working_memory.setdefault(channel_id, [])
        window.append(post)
        del window[: -config.max_context_messages]
        self.memory.update_profile_from_message(user_id, post["username"], post)
        log_event(log, "message.accepted", status="accepted", attachment_count=len(file_metas))
        approval = self.approval_command(message)
        if approval is not None:
            with log_context.bind(operation="approval"):
                await self._handle_approval_command(post, approval)
            return

        if self.is_explicit_invocation(post):
            typing_task = asyncio.create_task(self.delivery.typing_loop(channel_id))
            try:
                await self.respond(post, tag="mention", status_post_id=status_post_id)
            finally:
                typing_task.cancel()
            return

        with log_context.bind(operation="decide"):
            response = await self.decide_and_respond(post)
        if self.is_silent(response):
            log_event(log, "agent.silent", status="completed")
            return
        await self.delivery.reply(post, response)

    @staticmethod
    def _parse_post(event: dict) -> dict | None:
        raw = event.get("data", {}).get("post")
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            log.warning("无法解析 post JSON: %s", error)
            return None
        return parsed if isinstance(parsed, dict) and {"id", "message"} <= parsed.keys() else None

    def _accept(self, post: dict) -> bool:
        if post.get("user_id") == self.identity.user_id or post.get("type"):
            return False
        channel_id = str(post.get("channel_id") or "")
        if config.mm_channel_id and channel_id != config.mm_channel_id:
            return False
        if config.mm_team_id:
            return self.mm.get_channel(channel_id).get("team_id", "") == config.mm_team_id
        return True

    def is_explicit_invocation(self, post: dict) -> bool:
        message = str(post.get("message") or "").lower()
        if f"@{self.identity.username.lower()}" in message:
            return True
        if self.mm.get_channel(post["channel_id"]).get("type") == "D":
            return True
        root_id = str(post.get("root_id") or "")
        return bool(root_id and self.memory.get_post_user(root_id) == self.identity.user_id)

    async def decide_and_respond(self, post: dict) -> str:
        await self.delivery.typing_indicator(post["channel_id"])
        context = self.context_builder.build(post, mention=False)
        try:
            request = self.build_agent_request(post, "chat")
            selection = self.agent_router.default(request)
            runtime_request = self.build_run_request(
                post, context, capabilities=(), max_rounds=config.max_tool_rounds
            )
            result = await self.run_request(
                post,
                replace(request, intent=selection.intent, runtime_request=runtime_request),
                selection.agent,
            )
            return result.text or ""
        except AgentRuntimeError as error:
            log.error("LLM 决策异常: %s", error)
            return "<SILENT>"

    def build_run_request(
        self,
        post: dict,
        prompt_context: dict,
        *,
        capabilities: tuple[dict, ...],
        max_rounds: int,
        event_sink: RunEventSink | None = None,
    ) -> RunRequest:
        channel_id = post["channel_id"]
        team_id = self.mm.get_channel(channel_id).get("team_id") or "-"
        system_prompt = prompt_context["system"]
        return RunRequest(
            context=RunContext(
                trace_id=log_context.get("trace_id", "----"),
                actor_id=post.get("user_id", ""),
                conversation_id=channel_id,
                scope=f"mattermost:{team_id}/{channel_id}",
                deadline=datetime.now(UTC) + timedelta(seconds=config.runtime_deadline_seconds),
                run_id=self._run_id(post),
            ),
            messages=tuple(prompt_context["messages"]),
            system_prompt=system_prompt,
            capabilities=capabilities,
            max_rounds=max_rounds,
            event_sink=event_sink,
        )

    @staticmethod
    def is_silent(text: str) -> bool:
        if not text:
            return True
        return text.strip().split("\n", 1)[0].strip().startswith("<SILENT>")

    async def respond(
        self,
        post: dict,
        *,
        tag: str,
        max_rounds: int | None = None,
        status_post_id: str = "",
    ) -> None:
        started = time.monotonic()
        await self.delivery.typing_indicator(post["channel_id"])
        context = self.context_builder.build(post, mention=tag == "mention")
        rounds = max_rounds if max_rounds is not None else config.max_tool_rounds
        request = self.build_agent_request(post, tag)
        stream: MattermostStream | None = None
        try:
            selection = self.agent_router.route(request)
            if self._should_stream(selection.agent):
                stream = self.delivery.stream(
                    post,
                    self._run_id(post),
                    post_id=status_post_id,
                )
            request = replace(request, intent=selection.intent)
            request = self._resolve_skill(request, selection.agent)
            capability_names = self._effective_capabilities(request, selection.agent)
            runtime_request = self.build_run_request(
                post,
                context,
                capabilities=tuple(self.capability_registry.get_schema_list(capability_names)),
                max_rounds=rounds,
                event_sink=stream,
            )
            output = await self.run_request(
                post,
                replace(request, runtime_request=runtime_request),
                selection.agent,
            )
            runtime_result = output.runtime_result or AgentResult(
                text=output.text,
                runtime=f"agent:{output.agent_name}",
                artifacts=tuple(output.artifacts),
            )
            response_view = (
                self._register_approval_interrupt(
                    post,
                    runtime_result,
                    allowed_capabilities=capability_names,
                    allowed_execution_profiles=self._effective_execution_profiles(selection.agent),
                )
                if runtime_result.status is RuntimeStatus.WAITING_APPROVAL
                else self.presenter.present(
                    output,
                    run_id=self._run_id(post),
                )
            )
        except (AgentPackageError, AgentRuntimeError, SkillPackageError) as error:
            log_event(
                log,
                "request.failed",
                level=40,
                status="failed",
                error_code=type(error).__name__,
            )
            response_view = self._error_view(
                error,
                run_id=self._run_id(post),
            )
        response_length = len(response_view.summary)
        log_event(
            log,
            "agent.response_ready",
            status="completed",
            duration_ms=round((time.monotonic() - started) * 1000),
            output_size=response_length,
            invocation=tag,
        )
        update_post_id = status_post_id or (stream.post_id if stream is not None else "")
        await self.delivery.reply_view(
            post,
            response_view,
            update_post_id=update_post_id,
        )

    def _resolve_skill(self, request: AgentRequest, agent: ManagedAgent) -> AgentRequest:
        package = getattr(agent, "package", None)
        if package is None or not package.skills:
            return request
        invocation = self.skill_resolver.resolve(
            package,
            request,
            agent.descriptor.capabilities,
        )
        return replace(request, skill=invocation)

    @staticmethod
    def _effective_capabilities(
        request: AgentRequest,
        agent: ManagedAgent,
    ) -> tuple[str, ...]:
        if request.skill is not None:
            return request.skill.capabilities
        return agent.descriptor.capabilities

    @staticmethod
    def _effective_execution_profiles(agent: ManagedAgent) -> tuple[str, ...]:
        package = getattr(agent, "package", None)
        return tuple(package.execution_profiles) if package is not None else ()

    @staticmethod
    def _should_stream(agent: ManagedAgent) -> bool:
        if not config.mm_stream_enabled:
            return False
        package = getattr(agent, "package", None)
        if package is None:
            return True
        return package.manifest.runtime.mode == "agent"

    def build_agent_request(self, post: dict, intent: str) -> AgentRequest:
        return AgentRequest(
            intent=intent,
            prompt=str(post.get("message") or ""),
            scope=self.post_scope(post),
            permissions=frozenset({"web:read"}),
            budget_usd=config.model_budget_usd,
            actor_id=str(post.get("user_id") or ""),
            task_id=f"task:{post.get('id') or ''}",
            run_id=self._run_id(post),
        )

    def _register_approval_interrupt(
        self,
        post: dict,
        runtime_result: AgentResult,
        *,
        allowed_capabilities: tuple[str, ...] = (),
        allowed_execution_profiles: tuple[str, ...] = (),
    ) -> ResponseView:
        if not allowed_execution_profiles and runtime_result.interruptions:
            value = runtime_result.interruptions[0].get("value", {})
            if isinstance(value, dict):
                restored = value.get("execution_profiles", ())
                if isinstance(restored, (list, tuple)):
                    allowed_execution_profiles = tuple(
                        str(ref) for ref in restored if isinstance(ref, str) and ref
                    )
        scope = self.post_scope(post)
        approval = self.approval_coordinator.register(
            runtime_result,
            requested_by=post.get("user_id", ""),
            scope_id=scope,
            capability_context=CapabilityContext(
                trace_id=log_context.get("trace_id", "----"),
                actor_id=post.get("user_id", ""),
                conversation_id=post.get("channel_id", ""),
                message_id=post.get("id", ""),
                message=post.get("message", ""),
                scope=scope,
                allowed_capabilities=frozenset(allowed_capabilities),
                run_id=self._run_id(post),
                allowed_execution_profiles=frozenset(allowed_execution_profiles),
            ),
        )
        actions = self._approval_actions(post, approval.id)
        return self.presenter.approval(
            capability=approval.capability_name,
            approval_id=approval.id,
            run_id=self._run_id(post),
            actions=actions,
        )

    def _approval_actions(
        self, post: dict, approval_id: str
    ) -> tuple[ResponseAction, ...]:
        if self.action_tokens is None:
            return ()
        run_id = self._run_id(post)
        shared = {
            "target": approval_id,
            "scope_id": self.post_scope(post),
            "run_id": run_id,
            "conversation_id": str(post.get("channel_id") or ""),
            "root_id": self.delivery.thread_root(post),
            "requested_by": str(post.get("user_id") or ""),
        }
        try:
            approve = self.action_tokens.issue(action="approve", **shared)
            reject = self.action_tokens.issue(action="reject", **shared)
        except (TypeError, ValueError) as error:
            log.warning("Action token 创建失败，将使用文本降级: %s", error)
            return ()
        return (
            ResponseAction(
                id="approve",
                label="批准",
                action="approve",
                target=approval_id,
                style="success",
                fallback=f"`批准 {approval_id}`",
                token=approve,
            ),
            ResponseAction(
                id="reject",
                label="拒绝",
                action="reject",
                target=approval_id,
                style="danger",
                fallback=f"`拒绝 {approval_id}`",
                token=reject,
            ),
        )

    async def handle_action_callback(self, payload: dict) -> dict:
        if self.action_tokens is None:
            raise PermissionError("interactive actions are not configured")
        context = payload.get("context")
        token = context.get("token") if isinstance(context, dict) else ""
        actor_id = str(payload.get("user_id") or "")
        channel_id = str(payload.get("channel_id") or "")
        claims = self.action_tokens.verify(str(token or ""))
        if channel_id != claims.conversation_id:
            raise PermissionError("action belongs to another conversation")
        channel = self.mm.get_channel(channel_id)
        scope_id = f"mattermost:{channel.get('team_id') or '-'}/{channel_id}"
        if scope_id != claims.scope_id:
            raise PermissionError("action belongs to another scope")
        if claims.action not in {"approve", "reject"}:
            raise ValueError("action is not supported by this callback")
        approval = self.approval_coordinator.store.get_approval_request(claims.target)
        if not await self.approval_coordinator.authorizer.can_decide(approval, actor_id):
            raise PermissionError("actor is not authorized to decide this approval")
        claims = self.action_tokens.consume(str(token), actor_id=actor_id)
        task = asyncio.create_task(
            self._complete_action(claims, actor_id),
            name=f"action:{claims.jti}",
        )
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)
        decision = "已批准，正在继续执行。" if claims.action == "approve" else "已拒绝。"
        return {
            "update": {
                "message": decision,
                "props": {
                    "from_bot": "true",
                    "mmag_kind": "approval",
                    "mmag_status": "running" if claims.action == "approve" else "failed",
                },
            }
        }

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
                self._register_approval_interrupt(post, result)
                if result.status is RuntimeStatus.WAITING_APPROVAL
                else self.presenter.present(
                    AgentOutput(
                        text=result.text,
                        agent_name="approval",
                        artifacts=tuple(dict(item) for item in result.artifacts),
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

    def post_scope(self, post: dict) -> str:
        channel_id = str(post.get("channel_id") or "")
        team_id = self.mm.get_channel(channel_id).get("team_id") or "-"
        return f"mattermost:{team_id}/{channel_id}"

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
                self._register_approval_interrupt(post, result)
                if result.status is RuntimeStatus.WAITING_APPROVAL
                else self.presenter.present(
                    AgentOutput(
                        text=result.text,
                        agent_name="approval",
                        artifacts=tuple(dict(item) for item in result.artifacts),
                        runtime_result=result,
                    ),
                    run_id=self._run_id(post),
                )
            )
        except (KeyError, PermissionError, ValueError) as error:
            response_view = self.presenter.error(
                title="审批失败",
                summary="当前审批无法处理，请确认审批 ID、权限和状态。",
                run_id=self._run_id(post),
            )
            log.warning("审批处理失败: %s", error)
        await self.delivery.reply_view(post, response_view)

    @staticmethod
    def approval_command(message: str) -> tuple[bool, str] | None:
        parts = message.strip().split()
        if len(parts) != 2:
            return None
        if parts[0].lower() in {"批准", "approve"}:
            return True, parts[1]
        if parts[0].lower() in {"拒绝", "reject"}:
            return False, parts[1]
        return None

    async def run_request(
        self,
        post: dict,
        request: AgentRequest,
        agent: ManagedAgent,
    ) -> AgentOutput:
        runtime_request = request.runtime_request
        if not isinstance(runtime_request, RunRequest):
            raise TypeError("Mattermost execution requires a prepared RunRequest")
        allowed_capabilities = self._effective_capabilities(request, agent)
        package = getattr(agent, "package", None)
        started = time.monotonic()
        capability_context = CapabilityContext(
            trace_id=runtime_request.context.trace_id,
            actor_id=runtime_request.context.actor_id,
            conversation_id=runtime_request.context.conversation_id,
            message_id=post.get("id", ""),
            message=post.get("message", ""),
            scope=runtime_request.context.scope,
            allowed_capabilities=frozenset(allowed_capabilities),
            run_id=runtime_request.context.run_id,
            allowed_execution_profiles=frozenset(
                package.execution_profiles if package is not None else ()
            ),
        )
        with (
            bind_capability_context(capability_context),
            bind_governance_context(
                GovernanceContext(
                    capability_context.actor_id,
                    capability_context.scope,
                    resources={
                        "actor_id": capability_context.actor_id,
                        "conversation_id": capability_context.conversation_id,
                    },
                )
            ),
        ):
            lifecycle_run_id = self._lifecycle_run_id(capability_context.run_id)
            provenance = self._request_provenance(request, package, agent)
            self.audit_store.runs.bind_snapshot(
                lifecycle_run_id,
                snapshot=provenance,
                actor_id=capability_context.actor_id,
                trace_id=capability_context.trace_id,
                intent=request.intent,
                capabilities=allowed_capabilities,
            )
            log_event(
                log,
                "agent.started",
                status="running",
                intent=request.intent,
                agent_ref=agent.descriptor.name,
                skill_ref=request.skill.ref if request.skill is not None else "",
                package_hash=(package.snapshot.package_hash if package is not None else ""),
            )
            try:
                output = await agent.run(request)
            except Exception as error:
                duration_ms = round((time.monotonic() - started) * 1000)
                self.audit_store.runs.record_failure(
                    lifecycle_run_id,
                    error_code=type(error).__name__,
                )
                self.audit_store.append_audit(
                    "agent.run",
                    actor_id=capability_context.actor_id,
                    scope_id=capability_context.scope,
                    trace_id=capability_context.trace_id,
                    target=agent.descriptor.name,
                    decision="failed",
                    details={
                        "schema_version": "1.0",
                        "run_id": capability_context.run_id,
                        "intent": request.intent,
                        "skill_ref": request.skill.ref if request.skill is not None else "",
                        "duration_ms": duration_ms,
                        "error_code": type(error).__name__,
                        "route": (
                            package.manifest.runtime.route if package is not None else ""
                        ),
                        "model_policy_ref": (
                            package.manifest.model_policy_ref if package is not None else ""
                        ),
                        "provenance": self._request_provenance(request, package, agent),
                    },
                )
                log_event(
                    log,
                    "agent.failed",
                    level=40,
                    status="failed",
                    duration_ms=duration_ms,
                    error_code=type(error).__name__,
                    agent_ref=agent.descriptor.name,
                )
                raise
            provenance = self._output_provenance(output, request, package, agent)
            runtime_status = getattr(
                output.runtime_result,
                "status",
                RuntimeStatus.COMPLETED,
            )
            self.audit_store.append_audit(
                "agent.run",
                actor_id=capability_context.actor_id,
                scope_id=capability_context.scope,
                trace_id=capability_context.trace_id,
                target=agent.descriptor.name,
                decision=runtime_status.value,
                details={
                    "schema_version": "1.0",
                    "run_id": capability_context.run_id,
                    "message_id": capability_context.message_id,
                    "intent": request.intent,
                    "skill_ref": request.skill.ref if request.skill is not None else "",
                    "capabilities": list(allowed_capabilities),
                    "route": package.manifest.runtime.route if package is not None else "",
                    "model_policy_ref": (
                        package.manifest.model_policy_ref if package is not None else ""
                    ),
                    "provenance": dict(provenance),
                    "skill_context": self._interrupted_skill_context(output),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "usage": self._safe_usage(output),
                },
            )
            runtime_result = output.runtime_result
            self.audit_store.runs.record_result(
                lifecycle_run_id,
                status=runtime_status.value,
                usage=self._safe_usage(output),
                capability_calls=(
                    len(runtime_result.capability_calls) if runtime_result is not None else 1
                ),
                artifact_count=len(output.artifacts),
            )
            log_event(
                log,
                "agent.completed",
                status=runtime_status.value,
                agent_ref=output.agent_name,
                skill_ref=request.skill.ref if request.skill is not None else "",
                artifact_count=len(output.artifacts),
                runtime=getattr(output.runtime_result, "runtime", "deterministic"),
            )
        return output

    @staticmethod
    def _safe_usage(output: AgentOutput) -> dict[str, int | float]:
        runtime_result = output.runtime_result
        if runtime_result is None:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "model_calls": 0,
                "tool_calls": 0,
                "repair_calls": 0,
            }
        usage = runtime_result.usage
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "model_calls": usage.model_calls,
            "tool_calls": usage.tool_calls,
            "repair_calls": usage.repair_calls,
        }

    @staticmethod
    def _lifecycle_run_id(runtime_run_id: str) -> str:
        if runtime_run_id.startswith("mattermost:"):
            return f"run:{runtime_run_id.removeprefix('mattermost:')}"
        return runtime_run_id

    def _error_view(self, error: Exception, *, run_id: str) -> ResponseView:
        if isinstance(error, RuntimeTimeoutError):
            return self.presenter.error(
                title="执行超时",
                summary="任务未能在运行时限内完成，请缩小范围后重试。",
                run_id=run_id,
            )
        if isinstance(error, RuntimeRateLimitError):
            return self.presenter.error(
                title="服务繁忙",
                summary="模型服务当前限流，请稍后重试。",
                run_id=run_id,
            )
        if isinstance(error, RuntimeRejectedError):
            return self.presenter.error(
                title="请求未执行",
                summary="该请求不符合当前执行策略或权限边界。",
                run_id=run_id,
            )
        if isinstance(error, RuntimeUnavailableError):
            return self.presenter.error(
                title="外部服务不可用",
                summary="依赖服务暂时不可用，请稍后重试。",
                run_id=run_id,
            )
        if isinstance(error, (AgentPackageError, SkillPackageError)):
            return self.presenter.error(
                title="输入或结果不符合契约",
                summary="任务未通过 Agent/Skill 契约校验，请调整输入后重试。",
                run_id=run_id,
            )
        return self.presenter.error(
            title="系统故障",
            summary="任务未完成，详情已记录供运维查询。",
            run_id=run_id,
        )

    @staticmethod
    def _output_provenance(output, request: AgentRequest, package, agent) -> dict:
        if output.envelope:
            return dict(output.envelope.get("provenance", {}))
        return MessageHandler._request_provenance(request, package, agent)

    @staticmethod
    def _request_provenance(request: AgentRequest, package, agent) -> dict:
        provenance = package.snapshot.to_dict() if package is not None else {}
        provenance.update(dict(getattr(agent, "platform_provenance", {})))
        if request.skill is not None:
            provenance.update(request.skill.provenance)
        return provenance

    @staticmethod
    def _interrupted_skill_context(output) -> dict:
        runtime_result = output.runtime_result
        if runtime_result is None or not runtime_result.interruptions:
            return {}
        value = runtime_result.interruptions[0].get("value", {})
        if not isinstance(value, dict):
            return {}
        state = value.get("skill_context", {})
        return dict(state) if isinstance(state, dict) else {}
