"""Inbound Mattermost message orchestration through the managed-Agent router."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..agent_packages import AgentPackageError
from ..agent_system import AgentRequest
from ..capabilities import CapabilityContext, bind_capability_context
from ..config import config
from ..control_plane import InboundEvent, MessagePipeline, OutboundMessage
from ..governance import GovernanceContext, bind_governance_context
from ..logger import get_logger, trace
from ..runtimes import AgentResult, AgentRuntimeError, RunContext, RunRequest, RuntimeStatus
from ..skill_packages import SkillPackageError
from .delivery import OUTBOUND_COLLECTOR, MattermostDelivery

if TYPE_CHECKING:
    from ..agent_system import ManagedAgent
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
        self.pipeline: MessagePipeline | None = None

    async def on_posted(self, event: dict) -> None:
        if self.pipeline is None:
            await self.process_posted_event(event)
            return
        inbound = self.to_inbound_event(event)
        if inbound is not None:
            await self.pipeline.accept(inbound)

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
            log.info("⏭️ 跳过重复消息: %s", post_id[:12])
            return

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

        trace.new()
        trace.set_context(channel=channel_id[:12], user=post["username"], msg_type="mention")
        log.info("%s [%s] %s", trace.prefix(), post["username"], message[:80])
        approval = self.approval_command(message)
        if approval is not None:
            trace.set_context(msg_type="approval")
            await self._handle_approval_command(post, approval)
            trace.clear()
            return

        if self.is_explicit_invocation(post):
            trace.set_context(msg_type="mention")
            await self.delivery.send_ack(post)
            typing_task = asyncio.create_task(self.delivery.typing_loop(channel_id))
            try:
                await self.respond(post, tag="mention")
            finally:
                typing_task.cancel()
            trace.clear()
            return

        trace.set_context(msg_type="decide")
        response = await self.decide_and_respond(post)
        trace.clear()
        if self.is_silent(response):
            log.info("🤐 LLM 决定沉默: %s", message[:60])
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
        skill_context: str = "",
    ) -> RunRequest:
        channel_id = post["channel_id"]
        team_id = self.mm.get_channel(channel_id).get("team_id") or "-"
        system_prompt = prompt_context["system"]
        if skill_context:
            system_prompt = f"{system_prompt}\n\n## Active Skill\n{skill_context}"
        return RunRequest(
            context=RunContext(
                trace_id=trace.current,
                actor_id=post.get("user_id", ""),
                conversation_id=channel_id,
                scope=f"mattermost:{team_id}/{channel_id}",
                deadline=datetime.now(UTC) + timedelta(seconds=config.runtime_deadline_seconds),
                run_id=f"mattermost:{post.get('id', trace.current)}",
            ),
            messages=tuple(prompt_context["messages"]),
            system_prompt=system_prompt,
            capabilities=capabilities,
            max_rounds=max_rounds,
        )

    @staticmethod
    def is_silent(text: str) -> bool:
        if not text:
            return True
        return text.strip().split("\n", 1)[0].strip().startswith("<SILENT>")

    async def respond(self, post: dict, *, tag: str, max_rounds: int | None = None) -> None:
        started = time.monotonic()
        await self.delivery.typing_indicator(post["channel_id"])
        context = self.context_builder.build(post, mention=tag == "mention")
        rounds = max_rounds if max_rounds is not None else config.max_tool_rounds
        request = self.build_agent_request(post, tag)
        try:
            selection = self.agent_router.route(request)
            request = replace(request, intent=selection.intent)
            request = self._resolve_skill(request, selection.agent)
            capability_names = self._effective_capabilities(request, selection.agent)
            runtime_request = self.build_run_request(
                post,
                context,
                capabilities=tuple(self.capability_registry.get_schema_list(capability_names)),
                max_rounds=rounds,
                skill_context=request.skill.prompt_context if request.skill is not None else "",
            )
            result = await self.run_request(
                post,
                replace(request, runtime_request=runtime_request),
                selection.agent,
            )
            response = (
                self._register_approval_interrupt(
                    post,
                    result,
                    allowed_capabilities=capability_names,
                )
                if result.status is RuntimeStatus.WAITING_APPROVAL
                else result.text
            )
        except (AgentPackageError, AgentRuntimeError, SkillPackageError) as error:
            log.error("%s [%s] Agent 执行失败: %s", trace.prefix(), tag, error, exc_info=True)
            response = "⚠️ LLM 服务暂时不可用，请稍后再试。"
        log.info(
            "%s [%s] Agent 返回 (%.1fs, %d 字符)",
            trace.prefix(),
            tag,
            time.monotonic() - started,
            len(response),
        )
        if response:
            await self.delivery.reply(post, response)

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

    def build_agent_request(self, post: dict, intent: str) -> AgentRequest:
        return AgentRequest(
            intent=intent,
            prompt=str(post.get("message") or ""),
            scope=self.post_scope(post),
            permissions=frozenset({"web:read"}),
            budget_usd=config.model_budget_usd,
            actor_id=str(post.get("user_id") or ""),
            run_id=f"mattermost:{post.get('id', trace.current)}",
        )

    def _register_approval_interrupt(
        self,
        post: dict,
        runtime_result: AgentResult,
        *,
        allowed_capabilities: tuple[str, ...] = (),
    ) -> str:
        scope = self.post_scope(post)
        approval = self.approval_coordinator.register(
            runtime_result,
            requested_by=post.get("user_id", ""),
            scope_id=scope,
            capability_context=CapabilityContext(
                trace_id=trace.current,
                actor_id=post.get("user_id", ""),
                conversation_id=post.get("channel_id", ""),
                message_id=post.get("id", ""),
                message=post.get("message", ""),
                scope=scope,
                allowed_capabilities=frozenset(allowed_capabilities),
            ),
        )
        return (
            f"⏸️ 操作等待人工审批：`{approval.capability_name}`\n"
            f"审批 ID：`{approval.id}`\n"
            f"回复 `批准 {approval.id}` 或 `拒绝 {approval.id}`。"
        )

    def post_scope(self, post: dict) -> str:
        channel_id = str(post.get("channel_id") or "")
        team_id = self.mm.get_channel(channel_id).get("team_id") or "-"
        return f"mattermost:{team_id}/{channel_id}"

    async def _handle_approval_command(self, post: dict, command: tuple[bool, str]) -> None:
        approved, request_id = command
        try:
            result = await self.approval_coordinator.resume(
                request_id,
                approved=approved,
                actor_id=post.get("user_id", ""),
                scope_id=self.post_scope(post),
                trace_id=trace.current,
                reason="",
            )
            response = (
                self._register_approval_interrupt(post, result)
                if result.status is RuntimeStatus.WAITING_APPROVAL
                else result.text
            )
        except (KeyError, PermissionError, ValueError) as error:
            response = f"⚠️ 无法处理审批：{error}"
        await self.delivery.reply(post, response or "✅ 审批已处理。")

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
    ) -> AgentResult:
        runtime_request = request.runtime_request
        if not isinstance(runtime_request, RunRequest):
            raise TypeError("Mattermost execution requires a prepared RunRequest")
        allowed_capabilities = self._effective_capabilities(request, agent)
        capability_context = CapabilityContext(
            trace_id=runtime_request.context.trace_id,
            actor_id=runtime_request.context.actor_id,
            conversation_id=runtime_request.context.conversation_id,
            message_id=post.get("id", ""),
            message=post.get("message", ""),
            scope=runtime_request.context.scope,
            allowed_capabilities=frozenset(allowed_capabilities),
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
            package = getattr(agent, "package", None)
            log.info(
                "%s Agent route intent=%s agent=%s skill=%s package=%s",
                trace.prefix(),
                request.intent,
                agent.descriptor.name,
                request.skill.ref if request.skill is not None else "none",
                package.snapshot.package_hash[:12] if package is not None else "unpackaged",
            )
            output = await agent.run(request)
            provenance = self._output_provenance(output, request, package)
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
                    "intent": request.intent,
                    "skill_ref": request.skill.ref if request.skill is not None else "",
                    "capabilities": list(allowed_capabilities),
                    "provenance": dict(provenance),
                    "skill_resource_state": self._interrupted_resource_state(output),
                },
            )
            log.info(
                "%s Agent complete agent=%s skill=%s artifacts=%d runtime=%s",
                trace.prefix(),
                output.agent_name,
                request.skill.ref if request.skill is not None else "none",
                len(output.artifacts),
                getattr(output.runtime_result, "runtime", "deterministic"),
            )
        if output.runtime_result is not None:
            return output.runtime_result
        return AgentResult(
            text=output.text,
            runtime=f"agent:{output.agent_name}",
            artifacts=tuple(output.artifacts),
        )

    @staticmethod
    def _output_provenance(output, request: AgentRequest, package) -> dict:
        if output.envelope:
            return dict(output.envelope.get("provenance", {}))
        provenance = package.snapshot.to_dict() if package is not None else {}
        if request.skill is not None:
            provenance.update(request.skill.provenance)
        return provenance

    @staticmethod
    def _interrupted_resource_state(output) -> dict:
        runtime_result = output.runtime_result
        if runtime_result is None or not runtime_result.interruptions:
            return {}
        value = runtime_result.interruptions[0].get("value", {})
        if not isinstance(value, dict):
            return {}
        state = value.get("skill_resource_state", {})
        return dict(state) if isinstance(state, dict) else {}
