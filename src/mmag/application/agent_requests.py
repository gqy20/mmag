"""Agent request preparation, execution, governance, and presentation."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..agent_packages import AgentPackageError
from ..agent_system import AgentOutput, AgentRequest
from ..capabilities import CapabilityContext, bind_capability_context
from ..config import config
from ..control_plane import ScopeKind
from ..governance import GovernanceContext, bind_governance_context
from ..logger import get_logger, log_context, log_event, safe_hash
from ..runtimes import (
    AgentResult,
    AgentRuntimeError,
    RunContext,
    RunRequest,
    RuntimeRateLimitError,
    RuntimeRejectedError,
    RuntimeStatus,
    RuntimeTimeoutError,
    RuntimeUnavailableError,
)
from ..skill_packages import SkillPackageError
from .views import ResponseAction, ResponsePresenter, ResponseSection, ResponseView

if TYPE_CHECKING:
    from ..agent_system import ManagedAgent
    from ..runtimes import RunEventSink
    from ..skill_packages import SkillResolver
    from .context import ContextBuilder
    from .delivery import MattermostDelivery
    from .personal_ui import PersonalWorkspaceUI
    from .stream import MattermostStream

log = get_logger(__name__)


class AgentRequestHandler:
    def __init__(
        self,
        *,
        capability_registry,
        agent_router,
        skill_resolver: SkillResolver,
        audit_store,
        approval_coordinator,
        context_builder: ContextBuilder,
        delivery: MattermostDelivery,
        scope_resolver,
        personal_ui: PersonalWorkspaceUI | None,
        action_tokens,
        task_drafts=None,
    ) -> None:
        self.capability_registry = capability_registry
        self.agent_router = agent_router
        self.skill_resolver = skill_resolver
        self.audit_store = audit_store
        self.approval_coordinator = approval_coordinator
        self.context_builder = context_builder
        self.delivery = delivery
        self.scope_resolver = scope_resolver
        self.personal_ui = personal_ui
        self.action_tokens = action_tokens
        self.task_drafts = task_drafts
        self.presenter = ResponsePresenter()

    async def decide_and_respond(self, post: dict) -> str:
        await self.delivery.typing_indicator(post["channel_id"])
        context = self.context_builder.build(post, mention=False)
        try:
            request = self.build_agent_request(
                post, "chat", personal_preferences=context.get("personal_preferences")
            )
            selection = self.agent_router.default(request)
            self._record_agent_route(selection, request, invocation="ambient")
            capabilities = self._effective_capabilities(request, selection.agent)
            if self.scope_resolver.resolve_post(post).kind is ScopeKind.CHANNEL:
                capabilities = tuple(name for name in capabilities if name != "get_user_profile")
            self._record_tool_projection(selection.agent, request, capabilities)
            runtime_request = self.build_run_request(
                post,
                context,
                capabilities=tuple(self.capability_registry.get_schema_list(capabilities)),
                max_rounds=config.max_tool_rounds,
            )
            result = await self.run_request(
                post,
                replace(request, intent=selection.intent, runtime_request=runtime_request),
                selection.agent,
            )
            return result.text or ""
        except AgentRuntimeError as error:
            log.error("LLM 决策异常: %s", error)
            return self._runtime_error_text(error)

    async def respond(
        self,
        post: dict,
        *,
        tag: str,
        max_rounds: int | None = None,
        status_post_id: str = "",
        deliver: bool = True,
        stream_enabled: bool = True,
    ) -> ResponseView:
        started = time.monotonic()
        await self.delivery.typing_indicator(post["channel_id"])
        context = self.context_builder.build(post, mention=tag == "mention")
        rounds = max_rounds if max_rounds is not None else config.max_tool_rounds
        request = self.build_agent_request(
            post, tag, personal_preferences=context.get("personal_preferences")
        )
        stream: MattermostStream | None = None
        try:
            request = self.skill_resolver.prepare_personal_request(request)
            if post.get("_persona_ref"):
                request = replace(request, prompt=str(post.get("_persona_question") or ""))
                selection = self.agent_router.default(request)
            else:
                selection = self.agent_router.route(request)
            self._record_agent_route(selection, request, invocation=tag)
            if stream_enabled and self._should_stream(selection.agent):
                stream = self.delivery.stream(post, self.run_id(post), post_id=status_post_id)
            if not post.get("_persona_ref"):
                # Skill activation must inspect the originating intent and prompt. Replacing a
                # mention with the Agent's first accepted intent here would activate a domain
                # Skill merely because its Agent was selected by a different keyword family.
                request = self._resolve_skill(request, selection.agent)
            request = replace(request, intent=selection.intent)
            capabilities = self._effective_capabilities(request, selection.agent)
            if post.get("_persona_ref"):
                capabilities = ()
            if self.scope_resolver.resolve_post(post).kind is ScopeKind.CHANNEL:
                capabilities = tuple(name for name in capabilities if name != "get_user_profile")
            self._record_tool_projection(selection.agent, request, capabilities)
            runtime_request = self.build_run_request(
                post,
                context,
                capabilities=tuple(self.capability_registry.get_schema_list(capabilities)),
                max_rounds=rounds,
                event_sink=stream,
            )
            output = await self.run_request(
                post, replace(request, runtime_request=runtime_request), selection.agent
            )
            runtime_result = output.runtime_result or AgentResult(
                text=output.text,
                runtime=f"agent:{output.agent_name}",
                artifacts=tuple(output.artifacts),
            )
            response = (
                self.register_approval_interrupt(
                    post,
                    runtime_result,
                    allowed_capabilities=capabilities,
                    allowed_execution_profiles=self._effective_execution_profiles(selection.agent),
                )
                if runtime_result.status is RuntimeStatus.WAITING_APPROVAL
                else self.presenter.present(output, run_id=self.run_id(post))
            )
            if post.get("_persona_ref"):
                response = replace(
                    response,
                    title=f"{post.get('_persona_display_name')} · 数字人代理",
                    sections=(
                        *response.sections,
                        ResponseSection(
                            "代理声明",
                            items=(
                                "由 MMAG 根据所有者明确发布的资料快照生成，并非本人实时发言。",
                                f"数字人版本：`{post.get('_persona_ref')}`",
                            ),
                        ),
                    ),
                )
            elif self.personal_ui is not None and runtime_result.status is RuntimeStatus.COMPLETED:
                response = self.personal_ui.attach_work_case(
                    post,
                    self.scope_resolver.resolve_post(post),
                    request,
                    selection.agent.descriptor.name,
                    response,
                    provenance=self._output_provenance(
                        output, request, getattr(selection.agent, "package", None), selection.agent
                    ),
                )
            if self.task_drafts is not None and runtime_result.status is RuntimeStatus.COMPLETED:
                response = self.task_drafts.attach(post, output, response)
        except (AgentPackageError, AgentRuntimeError, SkillPackageError) as error:
            log_event(
                log,
                "request.failed",
                level=40,
                status="failed",
                error_code=type(error).__name__,
            )
            response = self.error_view(error, run_id=self.run_id(post))
        log_event(
            log,
            "agent.response_ready",
            status="completed",
            duration_ms=round((time.monotonic() - started) * 1000),
            output_size=len(response.summary),
            invocation=tag,
        )
        if deliver:
            await self.delivery.reply_view(
                post,
                response,
                update_post_id=status_post_id or (stream.post_id if stream is not None else ""),
            )
        return response

    async def respond_draft(self, post: dict, status_post_id: str) -> ResponseView:
        return await self.respond(
            post,
            tag="persona",
            status_post_id=status_post_id,
            deliver=False,
            stream_enabled=False,
        )

    def build_run_request(
        self,
        post: dict,
        prompt_context: dict,
        *,
        capabilities: tuple[dict, ...],
        max_rounds: int,
        event_sink: RunEventSink | None = None,
    ) -> RunRequest:
        scope = self.scope_resolver.resolve_post(post)
        return RunRequest(
            context=RunContext(
                trace_id=log_context.get("trace_id", "----"),
                actor_id=post.get("user_id", ""),
                conversation_id=post["channel_id"],
                scope=scope.id,
                deadline=datetime.now(UTC) + timedelta(seconds=config.runtime_deadline_seconds),
                run_id=self.run_id(post),
                installation_id=scope.installation_id,
                tenant_id=scope.tenant_id,
                scope_kind=scope.kind.value,
                owner_id=scope.owner_id,
                team_id=scope.team_id,
                channel_type=scope.channel_type,
            ),
            messages=tuple(prompt_context["messages"]),
            system_prompt=prompt_context["system"],
            capabilities=capabilities,
            max_rounds=max_rounds,
            event_sink=event_sink,
        )

    def build_agent_request(
        self, post: dict, intent: str, *, personal_preferences: object = None
    ) -> AgentRequest:
        preferences = personal_preferences if isinstance(personal_preferences, dict) else {}
        meeting_parameters = self._meeting_parameters(post) if intent == "mention" else None
        return AgentRequest(
            intent="meeting" if meeting_parameters is not None else intent,
            prompt=str(post.get("message") or ""),
            scope=self.post_scope(post),
            permissions=frozenset({"web:read"}),
            budget_usd=config.model_budget_usd,
            actor_id=str(post.get("user_id") or ""),
            task_id=f"task:{post.get('id') or ''}",
            run_id=self.run_id(post),
            preferred_agents=tuple(str(x) for x in preferences.get("preferred_agents", ())),
            preferred_skills=tuple(str(x) for x in preferences.get("preferred_skills", ())),
            requested_personal_skill=str(post.get("_requested_personal_skill") or ""),
            requested_agent="report" if meeting_parameters is not None else "",
            requested_skill="meeting" if meeting_parameters is not None else "",
            parameters=(
                {
                    "persona_ref": str(post.get("_persona_ref") or ""),
                    "persona_hash": str(post.get("_persona_hash") or ""),
                    "represented_owner": str(post.get("_represented_owner") or ""),
                }
                if post.get("_persona_ref")
                else meeting_parameters or {}
            ),
            response_style=str(preferences.get("response_style") or ""),
            language=str(preferences.get("language") or ""),
        )

    @staticmethod
    def _meeting_parameters(post: dict) -> dict[str, object] | None:
        supplied = post.get("_mmag_summary")
        if post.get("_mmag_entry") == "slash" and isinstance(supplied, dict):
            return dict(supplied)
        message = str(post.get("message") or "").lower()
        triggers = (
            "总结这个线程",
            "总结此线程",
            "总结一下这个讨论",
            "会议纪要",
            "总结最近",
            "总结今天的讨论",
            "提取结论和待办",
            "从这里开始总结",
            "summarize this thread",
            "meeting summary",
        )
        if not any(trigger in message for trigger in triggers):
            return None
        root_id = str(post.get("root_id") or "")
        if "从这里" in message:
            range_name = "since"
        elif "最近" in message or "今天" in message:
            range_name = "recent"
        else:
            range_name = "thread" if root_id else "recent"
        match = re.search(r"最近\s*(\d{1,2})\s*小时", message)
        hours = min(max(int(match.group(1)), 1), 24) if match else 2
        if "今天" in message:
            hours = 24
        return {
            "channel_id": str(post.get("channel_id") or ""),
            "range": range_name,
            "root_post_id": root_id,
            "anchor_post_id": str(post.get("id") or "") if range_name == "since" else "",
            "hours": hours,
            "limit": 100,
        }

    def register_approval_interrupt(
        self,
        post: dict,
        result: AgentResult,
        *,
        allowed_capabilities: tuple[str, ...] = (),
        allowed_execution_profiles: tuple[str, ...] = (),
    ) -> ResponseView:
        if not allowed_execution_profiles and result.interruptions:
            value = result.interruptions[0].get("value", {})
            restored = value.get("execution_profiles", ()) if isinstance(value, dict) else ()
            if isinstance(restored, (list, tuple)):
                allowed_execution_profiles = tuple(str(x) for x in restored if x)
        scope = self.scope_resolver.resolve_post(post)
        approval = self.approval_coordinator.register(
            result,
            requested_by=post.get("user_id", ""),
            scope_id=scope.id,
            capability_context=CapabilityContext(
                trace_id=log_context.get("trace_id", "----"),
                actor_id=post.get("user_id", ""),
                conversation_id=post.get("channel_id", ""),
                message_id=post.get("id", ""),
                message=post.get("message", ""),
                scope=scope.id,
                allowed_capabilities=frozenset(allowed_capabilities),
                run_id=self.run_id(post),
                allowed_execution_profiles=frozenset(allowed_execution_profiles),
                installation_id=scope.installation_id,
                tenant_id=scope.tenant_id,
                scope_kind=scope.kind.value,
                owner_id=scope.owner_id,
                team_id=scope.team_id,
                channel_type=scope.channel_type,
            ),
        )
        return self.presenter.approval(
            capability=approval.capability_name,
            approval_id=approval.id,
            run_id=self.run_id(post),
            actions=self._approval_actions(post, approval.id),
        )

    def _approval_actions(self, post: dict, approval_id: str) -> tuple[ResponseAction, ...]:
        if self.action_tokens is None:
            return ()
        shared = {
            "target": approval_id,
            "scope_id": self.post_scope(post),
            "run_id": self.run_id(post),
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
                "approve",
                "批准",
                "approve",
                approval_id,
                style="success",
                fallback=f"`批准 {approval_id}`",
                token=approve,
            ),
            ResponseAction(
                "reject",
                "拒绝",
                "reject",
                approval_id,
                style="danger",
                fallback=f"`拒绝 {approval_id}`",
                token=reject,
            ),
        )

    async def run_request(
        self, post: dict, request: AgentRequest, agent: ManagedAgent
    ) -> AgentOutput:
        runtime_request = request.runtime_request
        if not isinstance(runtime_request, RunRequest):
            raise TypeError("Mattermost execution requires a prepared RunRequest")
        capabilities = tuple(
            str(schema.get("name") or "")
            for schema in runtime_request.capabilities
            if str(schema.get("name") or "")
        )
        package = getattr(agent, "package", None)
        started = time.monotonic()
        context = CapabilityContext(
            trace_id=runtime_request.context.trace_id,
            actor_id=runtime_request.context.actor_id,
            conversation_id=runtime_request.context.conversation_id,
            message_id=post.get("id", ""),
            message=post.get("message", ""),
            scope=runtime_request.context.scope,
            allowed_capabilities=frozenset(capabilities),
            run_id=runtime_request.context.run_id,
            allowed_execution_profiles=frozenset(
                package.execution_profiles if package is not None else ()
            ),
            installation_id=runtime_request.context.installation_id,
            tenant_id=runtime_request.context.tenant_id,
            scope_kind=runtime_request.context.scope_kind,
            owner_id=runtime_request.context.owner_id,
            team_id=runtime_request.context.team_id,
            channel_type=runtime_request.context.channel_type,
        )
        with (
            bind_capability_context(context),
            bind_governance_context(
                GovernanceContext(
                    context.actor_id,
                    context.scope,
                    resources={
                        "actor_id": context.actor_id,
                        "conversation_id": context.conversation_id,
                        "installation_id": context.installation_id,
                        "tenant_id": context.tenant_id,
                        "scope_kind": context.scope_kind,
                        "owner_id": context.owner_id,
                        "team_id": context.team_id,
                        "channel_type": context.channel_type,
                    },
                )
            ),
        ):
            lifecycle_id = self._lifecycle_run_id(context.run_id)
            provenance = self._request_provenance(request, package, agent)
            self.audit_store.runs.bind_snapshot(
                lifecycle_id,
                snapshot=provenance,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
                intent=request.intent,
                capabilities=capabilities,
            )
            log_event(
                log,
                "agent.started",
                status="running",
                intent=request.intent,
                agent_ref=agent.descriptor.name,
                skill_ref=request.skill.ref if request.skill is not None else "",
                package_hash=package.snapshot.package_hash if package is not None else "",
            )
            try:
                output = await agent.run(request)
            except Exception as error:
                duration = round((time.monotonic() - started) * 1000)
                self.audit_store.runs.record_failure(lifecycle_id, error_code=type(error).__name__)
                self.audit_store.append_audit(
                    "agent.run",
                    actor_id=context.actor_id,
                    scope_id=context.scope,
                    trace_id=context.trace_id,
                    target=agent.descriptor.name,
                    decision="failed",
                    details={
                        "schema_version": "1.0",
                        "run_id": context.run_id,
                        "intent": request.intent,
                        "skill_ref": request.skill.ref if request.skill is not None else "",
                        "duration_ms": duration,
                        "error_code": type(error).__name__,
                        "route": package.manifest.runtime.route if package is not None else "",
                        "model_policy_ref": package.manifest.model_policy_ref
                        if package is not None
                        else "",
                        "provenance": provenance,
                    },
                )
                log_event(
                    log,
                    "agent.failed",
                    level=40,
                    status="failed",
                    duration_ms=duration,
                    error_code=type(error).__name__,
                    error_message=str(error)[:500],
                    agent_ref=agent.descriptor.name,
                )
                raise
            provenance = self._output_provenance(output, request, package, agent)
            status = getattr(output.runtime_result, "status", RuntimeStatus.COMPLETED)
            self.audit_store.append_audit(
                "agent.run",
                actor_id=context.actor_id,
                scope_id=context.scope,
                trace_id=context.trace_id,
                target=agent.descriptor.name,
                decision=status.value,
                details={
                    "schema_version": "1.0",
                    "run_id": context.run_id,
                    "message_id": context.message_id,
                    "intent": request.intent,
                    "skill_ref": request.skill.ref if request.skill is not None else "",
                    "capabilities": list(capabilities),
                    "route": package.manifest.runtime.route if package is not None else "",
                    "model_policy_ref": package.manifest.model_policy_ref
                    if package is not None
                    else "",
                    "provenance": dict(provenance),
                    "skill_context": self._interrupted_skill_context(output),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "usage": self._safe_usage(output),
                },
            )
            runtime_result = output.runtime_result
            self.audit_store.runs.record_result(
                lifecycle_id,
                status=status.value,
                usage=self._safe_usage(output),
                capability_calls=len(runtime_result.capability_calls)
                if runtime_result is not None
                else 1,
                artifact_count=len(output.artifacts),
            )
            log_event(
                log,
                "agent.completed",
                status=status.value,
                agent_ref=output.agent_name,
                skill_ref=request.skill.ref if request.skill is not None else "",
                artifact_count=len(output.artifacts),
                runtime=getattr(output.runtime_result, "runtime", "deterministic"),
            )
        return output

    def error_view(self, error: Exception, *, run_id: str) -> ResponseView:
        mapping = (
            (RuntimeTimeoutError, "执行超时", "任务未能在运行时限内完成，请缩小范围后重试。"),
            (RuntimeRateLimitError, "服务繁忙", "模型服务当前限流，请稍后重试。"),
            (RuntimeRejectedError, "请求未执行", "该请求不符合当前执行策略或权限边界。"),
            (RuntimeUnavailableError, "外部服务不可用", "依赖服务暂时不可用，请稍后重试。"),
        )
        for kind, title, summary in mapping:
            if isinstance(error, kind):
                return self.presenter.error(title=title, summary=summary, run_id=run_id)
        if isinstance(error, (AgentPackageError, SkillPackageError)):
            return self.presenter.error(
                title="输入或结果不符合契约",
                summary="任务未通过 Agent/Skill 契约校验，请调整输入后重试。",
                run_id=run_id,
            )
        return self.presenter.error(
            title="系统故障", summary="任务未完成，详情已记录供运维查询。", run_id=run_id
        )

    @staticmethod
    def _runtime_error_text(error: AgentRuntimeError) -> str:
        mapping = (
            (RuntimeTimeoutError, "执行超时，请缩小范围后重试。"),
            (RuntimeRateLimitError, "模型服务当前限流，请稍后重试。"),
            (RuntimeRejectedError, "请求未执行：不符合当前执行策略或权限边界。"),
            (RuntimeUnavailableError, "依赖服务暂时不可用，请稍后重试。"),
        )
        for kind, summary in mapping:
            if isinstance(error, kind):
                return summary
        return "处理失败，详情已记录供运维查询。"

    def post_scope(self, post: dict) -> str:
        return self.scope_resolver.resolve_post(post).id

    @staticmethod
    def run_id(post: dict) -> str:
        root_id = str(post.get("root_id") or "")
        thread = root_id if root_id else str(post.get("id") or log_context.get("trace_id", "unknown"))
        return f"mattermost:{thread}"

    @staticmethod
    def is_silent(text: str) -> bool:
        return not text or text.strip().split("\n", 1)[0].strip().startswith("<SILENT>")

    def _resolve_skill(self, request: AgentRequest, agent: ManagedAgent) -> AgentRequest:
        package = getattr(agent, "package", None)
        if package is None or not package.skills:
            self._record_skill_route(request, agent, None, reason="not_configured")
            return request
        invocation = self.skill_resolver.resolve(package, request, agent.descriptor.capabilities)
        self._record_skill_route(
            request,
            agent,
            invocation,
            reason=("explicit" if request.requested_skill else "activation")
            if invocation is not None
            else "no_match",
        )
        return replace(request, skill=invocation)

    def _record_agent_route(self, selection, request: AgentRequest, *, invocation: str) -> None:
        descriptor = selection.agent.descriptor
        fields = {
            "agent_ref": descriptor.name,
            "run_id": request.run_id,
            "invocation": invocation,
            "originating_intent": request.intent,
            "selected_intent": selection.intent,
            "reason": selection.reason,
            "matched_keywords": selection.matched_keywords,
            "candidate_count": selection.candidate_count,
        }
        log_event(log, "agent.route.selected", status="selected", **fields)
        self._append_routing_audit(
            "agent.route",
            request,
            target=descriptor.name,
            decision="selected",
            details={key: value for key, value in fields.items() if key != "agent_ref"},
        )

    def _record_skill_route(
        self,
        request: AgentRequest,
        agent: ManagedAgent,
        invocation,
        *,
        reason: str,
    ) -> None:
        selected_ref = invocation.ref if invocation is not None else ""
        decision = "selected" if invocation is not None else "skipped"
        log_event(
            log,
            f"skill.route.{decision}",
            status=decision,
            agent_ref=agent.descriptor.name,
            skill_ref=selected_ref,
            run_id=request.run_id,
            originating_intent=request.intent,
            reason=reason,
        )
        self._append_routing_audit(
            "skill.route",
            request,
            target=selected_ref or agent.descriptor.name,
            decision=decision,
            details={
                "run_id": request.run_id,
                "agent_ref": agent.descriptor.name,
                "skill_ref": selected_ref,
                "originating_intent": request.intent,
                "reason": reason,
            },
        )

    def _record_tool_projection(
        self,
        agent: ManagedAgent,
        request: AgentRequest,
        capabilities: tuple[str, ...],
    ) -> None:
        names = tuple(sorted(capabilities))
        log_event(
            log,
            "agent.tools.projected",
            status="ready",
            agent_ref=agent.descriptor.name,
            skill_ref=request.skill.ref if request.skill is not None else "",
            run_id=request.run_id,
            capability_count=len(names),
            capability_names=names,
            capability_set_sha256=safe_hash(names),
        )

    def _append_routing_audit(
        self,
        event_type: str,
        request: AgentRequest,
        *,
        target: str,
        decision: str,
        details: dict,
    ) -> None:
        try:
            self.audit_store.append_audit(
                event_type,
                actor_id=request.actor_id,
                scope_id=request.scope,
                trace_id=log_context.get("trace_id"),
                target=target,
                decision=decision,
                details={"schema_version": "1.0", **details},
            )
        except Exception as error:
            log_event(
                log,
                "audit.write_failed",
                level=40,
                status="degraded",
                error_code=type(error).__name__,
                audit_event_type=event_type,
            )

    @staticmethod
    def _effective_capabilities(request: AgentRequest, agent: ManagedAgent) -> tuple[str, ...]:
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
        return package is None or package.manifest.runtime.mode == "agent"

    @staticmethod
    def _safe_usage(output: AgentOutput) -> dict[str, int | float]:
        if output.runtime_result is None:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "model_calls": 0,
                "tool_calls": 0,
                "repair_calls": 0,
            }
        usage = output.runtime_result.usage
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "model_calls": usage.model_calls,
            "tool_calls": usage.tool_calls,
            "repair_calls": usage.repair_calls,
        }

    @staticmethod
    def _lifecycle_run_id(run_id: str) -> str:
        return (
            f"run:{run_id.removeprefix('mattermost:')}"
            if run_id.startswith("mattermost:")
            else run_id
        )

    @staticmethod
    def _output_provenance(output, request: AgentRequest, package, agent) -> dict:
        return (
            dict(output.envelope.get("provenance", {}))
            if output.envelope
            else AgentRequestHandler._request_provenance(request, package, agent)
        )

    @staticmethod
    def _request_provenance(request: AgentRequest, package, agent) -> dict:
        provenance = package.snapshot.to_dict() if package is not None else {}
        provenance.update(dict(getattr(agent, "platform_provenance", {})))
        if request.skill is not None:
            provenance.update(request.skill.provenance)
        for key in ("persona_ref", "persona_hash", "represented_owner"):
            if request.parameters.get(key):
                provenance[key] = str(request.parameters[key])
        return provenance

    @staticmethod
    def _interrupted_skill_context(output) -> dict:
        result = output.runtime_result
        if result is None or not result.interruptions:
            return {}
        value = result.interruptions[0].get("value", {})
        state = value.get("skill_context", {}) if isinstance(value, dict) else {}
        return dict(state) if isinstance(state, dict) else {}
