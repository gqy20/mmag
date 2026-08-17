"""Mattermost-native presentation and signed actions for the small Goal model."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..capabilities import (
    CapabilityContext,
    CapabilityStatus,
    bind_capability_context,
)
from ..governance import GovernanceContext, bind_governance_context
from ..logger import log_context, log_event, safe_hash
from .views import (
    ResponseAction,
    ResponseKind,
    ResponseSection,
    ResponseView,
    RunStatus,
)

if TYPE_CHECKING:
    from ..agent_system import AgentOutput
    from ..control_plane import MattermostAccessGuard, Scope

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_GOAL_CAPABILITIES = frozenset(
    {"create_goal", "list_goals", "update_goal", "get_goal_overview"}
)
_STATUS_LABELS = {
    "draft": "草稿",
    "active": "进行中",
    "completed": "已完成",
    "cancelled": "已取消",
}
log = logging.getLogger(__name__)


class GoalWorkspaceUI:
    """Project Goal view backed only by trusted capability results and actions."""

    def __init__(
        self,
        *,
        mm_client,
        capability_registry,
        access_guard: MattermostAccessGuard,
        scope_resolver,
        action_tokens,
        audit_store,
        policy_ref: str,
        allowed_capabilities: tuple[str, ...],
    ) -> None:
        self.mm = mm_client
        self.capability_registry = capability_registry
        self.access_guard = access_guard
        self.scope_resolver = scope_resolver
        self.action_tokens = action_tokens
        self.audit_store = audit_store
        self.policy_ref = policy_ref
        self.allowed_capabilities = tuple(
            name for name in allowed_capabilities if name in _GOAL_CAPABILITIES
        )

    def attach(self, post: dict, output: AgentOutput, view: ResponseView) -> ResponseView:
        observed = self._observed_call(output)
        if observed is None or view.status is not RunStatus.SUCCEEDED:
            return view
        capability, payload = observed
        return self._view(
            capability,
            payload,
            post=post,
            run_id=view.run_id,
        )

    async def handle_action(
        self,
        claims,
        *,
        actor_id: str,
        scope: Scope,
    ) -> ResponseView:
        await self.access_guard.require(
            actor_id,
            scope.id,
            channel_id=claims.conversation_id,
        )
        if claims.action in {"goal_view", "goal_complete", "goal_cancel"}:
            capability = "get_goal_overview"
            arguments = {"goal_id": claims.target}
        elif claims.action == "goal_complete_confirm":
            capability = "update_goal"
            arguments = {"goal_id": claims.target, "status": "completed"}
        elif claims.action == "goal_cancel_confirm":
            capability = "update_goal"
            arguments = {"goal_id": claims.target, "status": "cancelled"}
        else:
            raise ValueError("unsupported Goal action")
        if capability not in self.allowed_capabilities:
            raise PermissionError("Goal action is outside the Project Agent allowlist")

        context = CapabilityContext(
            trace_id=claims.run_id,
            actor_id=actor_id,
            conversation_id=claims.conversation_id,
            message_id=claims.root_id,
            message=f"Mattermost Goal action: {claims.action}",
            scope=scope.id,
            allowed_capabilities=frozenset(self.allowed_capabilities),
            run_id=claims.run_id,
            installation_id=scope.installation_id,
            tenant_id=scope.tenant_id,
            scope_kind=scope.kind.value,
            owner_id=scope.owner_id,
            team_id=scope.team_id,
            channel_type=scope.channel_type,
            tool_call_id=claims.jti,
            execution_key=f"goal-action:{claims.jti}",
        )
        governance = GovernanceContext(
            actor_id,
            scope.id,
            resources={
                "actor_id": actor_id,
                "conversation_id": claims.conversation_id,
                "installation_id": scope.installation_id,
                "tenant_id": scope.tenant_id,
                "scope_kind": scope.kind.value,
                "owner_id": scope.owner_id,
                "team_id": scope.team_id,
                "channel_type": scope.channel_type,
            },
            policy_ref=self.policy_ref,
            allowed_capabilities=self.allowed_capabilities,
        )
        with (
            log_context.bind(
                trace_id=claims.run_id,
                run_id=claims.run_id,
                actor_id=actor_id,
                conversation_id=claims.conversation_id,
                policy_ref=self.policy_ref,
            ),
            bind_capability_context(context),
            bind_governance_context(governance),
        ):
            result = await self.capability_registry.execute(capability, arguments)
        if result.status is not CapabilityStatus.SUCCESS or not isinstance(result.data, dict):
            raise RuntimeError(result.message or "Goal action failed")
        if result.data.get("status") == "error":
            return self._error(str(result.data.get("error") or "目标操作未完成"), claims.run_id)
        self.audit_store.append_audit(
            "goal.action",
            actor_id=actor_id,
            scope_id=scope.id,
            trace_id=claims.run_id,
            target=claims.target,
            decision="completed",
            details={"schema_version": "1.0", "action": claims.action},
        )
        log_event(
            log,
            "goal.action",
            status="completed",
            action=claims.action,
            goal_id_sha256=safe_hash(claims.target),
        )
        post = {
            "id": claims.root_id,
            "root_id": claims.root_id,
            "channel_id": claims.conversation_id,
            "user_id": actor_id,
        }
        if claims.action in {"goal_complete", "goal_cancel"}:
            return self._confirmation(
                claims.action,
                result.data,
                post=post,
                run_id=claims.run_id,
            )
        return self._view(capability, result.data, post=post, run_id=claims.run_id)

    def _view(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        post: dict,
        run_id: str,
    ) -> ResponseView:
        if payload.get("status") == "error":
            return self._error(str(payload.get("error") or "目标操作未完成"), run_id)
        if capability == "list_goals":
            goals = tuple(
                self._goal_line(goal)
                for goal in payload.get("goals", ())
                if isinstance(goal, dict)
            )
            return ResponseView(
                kind=ResponseKind.RESULT,
                title="目标列表",
                summary=f"当前共有 {len(goals)} 个符合条件的目标。",
                status=RunStatus.SUCCEEDED,
                run_id=run_id,
                sections=(ResponseSection("目标", items=goals),) if goals else (),
            )

        goal = payload.get("goal")
        if not isinstance(goal, dict):
            return self._error("没有取得可展示的目标数据。", run_id)
        title = {
            "create_goal": "目标已创建",
            "get_goal_overview": "目标进度",
        }.get(capability, self._update_title(goal))
        sections = [ResponseSection("", body=self._overview_line(goal))]
        criteria = self._criteria_lines(goal)
        if len(criteria) == 1:
            sections.append(ResponseSection("", body=f"成功标准：{criteria[0]}"))
        elif criteria:
            sections.append(ResponseSection("成功标准", items=criteria))
        if capability == "get_goal_overview":
            total = int(payload.get("task_count") or 0)
            completed = int(payload.get("completed_task_count") or 0)
            ratio = min(1.0, max(0.0, float(payload.get("completion_ratio") or 0)))
            sections.insert(
                1,
                ResponseSection(
                    "",
                    body=(f"进度 **{round(ratio * 100)}%** · "
                          f"{completed}/{total} 项  {self._progress_bar(ratio)}"),
                ),
            )
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=title,
            summary=f"**{str(goal.get('title') or '未命名目标')}**",
            status=RunStatus.SUCCEEDED,
            run_id=run_id,
            sections=tuple(sections),
            actions=self._actions(post, goal, run_id),
            status_icon=self._status_icon(goal),
        )

    def _actions(
        self,
        post: dict,
        goal: dict[str, Any],
        run_id: str,
    ) -> tuple[ResponseAction, ...]:
        if self.action_tokens is None:
            return ()
        goal_id = str(goal.get("id") or "")
        status = str(goal.get("status") or "active")
        if not goal_id or status in {"completed", "cancelled"}:
            return ()
        scope = self.scope_resolver.resolve_post(post)
        shared = {
            "target": goal_id,
            "scope_id": scope.id,
            "run_id": run_id,
            "conversation_id": str(post.get("channel_id") or ""),
            "root_id": str(post.get("root_id") or post.get("id") or ""),
            "requested_by": str(post.get("user_id") or ""),
        }
        actions = [
            ResponseAction(
                "goal-view",
                "查看进度",
                "goal_view",
                goal_id,
                token=self.action_tokens.issue(action="goal_view", **shared),
            )
        ]
        if status == "active":
            actions.append(
                ResponseAction(
                    "goal-complete",
                    "标记完成",
                    "goal_complete",
                    goal_id,
                    style="success",
                    token=self.action_tokens.issue(action="goal_complete", **shared),
                )
            )
        actions.append(
            ResponseAction(
                "goal-cancel",
                "取消目标",
                "goal_cancel",
                goal_id,
                style="danger",
                token=self.action_tokens.issue(action="goal_cancel", **shared),
            )
        )
        return tuple(actions)

    def _confirmation(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        post: dict,
        run_id: str,
    ) -> ResponseView:
        goal = payload.get("goal")
        if not isinstance(goal, dict):
            return self._error("没有取得可展示的目标数据。", run_id)
        goal_id = str(goal.get("id") or "")
        if not goal_id:
            return self._error("目标缺少可操作标识。", run_id)
        scope = self.scope_resolver.resolve_post(post)
        shared = {
            "target": goal_id,
            "scope_id": scope.id,
            "run_id": run_id,
            "conversation_id": str(post.get("channel_id") or ""),
            "root_id": str(post.get("root_id") or post.get("id") or ""),
            "requested_by": str(post.get("user_id") or ""),
        }
        completing = action == "goal_complete"
        verb = "完成" if completing else "取消"
        confirm_action = "goal_complete_confirm" if completing else "goal_cancel_confirm"
        return ResponseView(
            kind=ResponseKind.APPROVAL,
            title=f"确认{verb}目标",
            summary=f"**{str(goal.get('title') or '未命名目标')}**",
            status=RunStatus.WAITING_APPROVAL,
            run_id=run_id,
            sections=(
                ResponseSection(
                    "",
                    body=f"{verb}后不能重新激活。",
                ),
            ),
            actions=(
                ResponseAction(
                    f"{confirm_action}-button",
                    f"确认{verb}",
                    confirm_action,
                    goal_id,
                    style="danger" if not completing else "success",
                    token=self.action_tokens.issue(action=confirm_action, **shared),
                ),
                ResponseAction(
                    "goal-view-back",
                    "返回",
                    "goal_view",
                    goal_id,
                    token=self.action_tokens.issue(action="goal_view", **shared),
                ),
            ),
            status_icon="⚠️",
        )

    @staticmethod
    def _observed_call(output: AgentOutput) -> tuple[str, dict[str, Any]] | None:
        runtime_result = output.runtime_result
        calls = getattr(runtime_result, "capability_calls", ())
        for call in reversed(calls):
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            result = call.get("result")
            if name in _GOAL_CAPABILITIES and isinstance(result, dict):
                return name, result
        return None

    def _overview_line(self, goal: dict[str, Any]) -> str:
        status = _STATUS_LABELS.get(str(goal.get("status") or ""), "未知")
        owner_id = str(goal.get("owner_id") or "")
        owner = "未指定"
        if owner_id:
            try:
                owner = str(self.mm.get_username(owner_id) or "已指定")
            except Exception:
                owner = "已指定"
        return f"**{status}** · 负责人 {owner} · 截止 {self._time(goal.get('due_time'))}"

    @staticmethod
    def _status_icon(goal: dict[str, Any]) -> str:
        return {
            "draft": "📝",
            "active": "🎯",
            "completed": "✅",
            "cancelled": "🚫",
        }.get(str(goal.get("status") or ""), "🎯")

    @staticmethod
    def _criteria_lines(goal: dict[str, Any]) -> tuple[str, ...]:
        lines: list[str] = []
        for item in goal.get("success_criteria", ()):
            if not isinstance(item, dict) or not str(item.get("description") or "").strip():
                continue
            line = str(item["description"]).strip()
            current = item.get("current_value")
            target = item.get("target_value")
            unit = str(item.get("unit") or "")
            if target is not None:
                prefix = f"当前 {current}{unit} · " if current is not None else ""
                line += f"（{prefix}目标 {target}{unit}）"
            lines.append(line)
        return tuple(lines)

    @staticmethod
    def _goal_line(goal: dict[str, Any]) -> str:
        title = str(goal.get("title") or "未命名目标")
        status = _STATUS_LABELS.get(str(goal.get("status") or ""), "未知")
        return f"**{title}** · {status}"

    @staticmethod
    def _update_title(goal: dict[str, Any]) -> str:
        return {
            "completed": "目标已完成",
            "cancelled": "目标已取消",
        }.get(str(goal.get("status") or ""), "目标已更新")

    @staticmethod
    def _progress_bar(ratio: float) -> str:
        filled = round(ratio * 10)
        return "`" + "█" * filled + "░" * (10 - filled) + "`"

    @staticmethod
    def _time(value: Any) -> str:
        try:
            timestamp = float(value or 0)
        except (TypeError, ValueError):
            return "未设置"
        if timestamp <= 0:
            return "未设置"
        # Model/tool contracts in the current project accept numeric timestamps, while
        # existing callers contain both Unix seconds and Mattermost-style milliseconds.
        if timestamp >= 100_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, _SHANGHAI).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return "未设置"

    @staticmethod
    def _error(message: str, run_id: str) -> ResponseView:
        message = re.sub(r"\bgoal_[A-Za-z0-9_-]+\b", "该目标", message)
        return ResponseView(
            kind=ResponseKind.ERROR,
            title="目标操作未完成",
            summary=message,
            status=RunStatus.FAILED,
            run_id=run_id,
        )
