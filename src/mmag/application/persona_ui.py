"""Mattermost ownership and query flows for governed Digital Personas."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..control_plane import (
    DigitalPersonaStatus,
    PersonaReplyRequest,
    PersonaReplyState,
    ScopeKind,
)
from .views import ResponseAction, ResponseKind, ResponseSection, ResponseView, RunStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..control_plane import DigitalPersonaStore, MemoryItemStore, PersonaReplyStore, Scope
    from .actions import ActionClaims, ActionTokenService


@dataclass(frozen=True, slots=True)
class PersonaInvocation:
    persona_ref: str
    display_name: str
    represented_owner: str
    persona_hash: str
    question: str
    approval_required: bool = False
    approval_reason: str = ""


_DEFAULT_APPROVAL_TOPICS = (
    "报价", "价格", "承诺", "合同", "签约", "付款", "薪酬", "绩效", "招聘",
    "解雇", "法务", "投资", "采购", "对外发布",
)


class PersonaWorkspaceUI:
    def __init__(
        self,
        *,
        personas: DigitalPersonaStore,
        memories: MemoryItemStore,
        action_tokens: ActionTokenService | None,
        reply_requests: PersonaReplyStore | None = None,
        audit_store=None,
    ) -> None:
        self.personas = personas
        self.memories = memories
        self.action_tokens = action_tokens
        self.reply_requests = reply_requests
        self.audit_store = audit_store

    def consume_owner(
        self, post: dict, message: str, scope: Scope
    ) -> tuple[bool, ResponseView | None]:
        if scope.kind is not ScopeKind.PERSONAL:
            return False, None
        normalized = re.sub(r"\s+", "", message).lower()
        if normalized in {"我的数字人", "管理我的数字人", "管理数字人", "mypersona"}:
            return True, self.view(post, scope)
        if normalized.startswith("创建我的数字人"):
            existing = self.personas.list_latest_owner(
                installation_id=scope.installation_id,
                tenant_id=scope.tenant_id,
                owner_id=scope.owner_id,
            )
            if existing:
                return True, self.view(post, scope)
            allowed, approval, denied = self._topics(message)
            username = str(post.get("username") or scope.owner_id[:8]).lstrip("@")
            persona = self.personas.create_revision(
                installation_id=scope.installation_id,
                tenant_id=scope.tenant_id,
                owner_id=scope.owner_id,
                owner_username=username,
                scope_id=scope.id,
                display_name=f"{username}的数字人",
                allowed_topics=allowed,
                approval_topics=approval,
                denied_topics=denied,
            )
            self._audit("persona.revision", post, scope, persona.ref, "created")
            return True, self.view(post, scope)
        parts = message.strip().split(maxsplit=1)
        commands = {
            "授权资料": "persona_add",
            "发布数字人": "persona_publish",
            "暂停数字人": "persona_archive",
        }
        if len(parts) == 2 and parts[0] in commands:
            try:
                result = self._execute(
                    commands[parts[0]], parts[1].strip(),
                    actor_id=scope.owner_id, post=post, scope=scope,
                )
            except (KeyError, PermissionError, ValueError) as error:
                return True, self._error("操作未完成", str(error))
            return True, self._status("操作已受理", result)
        return False, None

    def resolve_question(
        self, message: str, *, installation_id: str, tenant_id: str
    ) -> tuple[PersonaInvocation | None, ResponseView | None]:
        cleaned = re.sub(r"@[A-Za-z0-9_.-]+", "", message).strip()
        match = re.search(
            r"(?:问一下|问问|咨询)\s*@?(.{1,40}?)的数字人\s*[：:，,]\s*(.+)",
            cleaned,
            re.S,
        )
        if match is None:
            return None, None
        target, question = match.group(1).strip(), match.group(2).strip()
        matches = self.personas.find_active(
            target, installation_id=installation_id, tenant_id=tenant_id
        )
        if not matches:
            return None, self._status("没有找到数字人", f"没有找到已发布的“{target}”数字人。")
        if len(matches) > 1:
            names = "、".join(persona.display_name for persona in matches[:5])
            return None, self._status("需要明确目标", f"找到多个数字人：{names}。")
        persona = matches[0]
        denied = next(
            (topic for topic in persona.denied_topics if topic and topic in question), ""
        )
        if denied:
            return None, ResponseView(
                kind=ResponseKind.ERROR,
                title=f"{persona.display_name} · 拒绝回答",
                summary=f"该问题涉及数字人所有者禁止回答的主题：{denied}。",
                status=RunStatus.FAILED,
            )
        if persona.allowed_topics and not any(
            topic and topic in question for topic in persona.allowed_topics
        ):
            return None, ResponseView(
                kind=ResponseKind.ERROR,
                title=f"{persona.display_name} · 超出回答范围",
                summary="该问题不在数字人所有者允许回答的主题内。",
                status=RunStatus.FAILED,
            )
        approval_topic = next(
            (
                topic
                for topic in (*persona.approval_topics, *_DEFAULT_APPROVAL_TOPICS)
                if topic and topic in question
            ),
            "",
        )
        approval_required = persona.response_mode == "owner_approval" or (
            persona.response_mode == "risk_approval" and bool(approval_topic)
        )
        reason = (
            "数字人配置要求所有回答由本人确认"
            if persona.response_mode == "owner_approval"
            else f"问题涉及高风险主题：{approval_topic}"
            if approval_required
            else ""
        )
        return PersonaInvocation(
            persona.ref,
            persona.display_name,
            persona.owner_id,
            persona.sha256,
            question,
            approval_required,
            reason,
        ), None

    def view(self, post: dict, scope: Scope) -> ResponseView:
        personas = self.personas.list_latest_owner(
            installation_id=scope.installation_id,
            tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
        )
        if not personas:
            return self._status("我的数字人", "还没有数字人，发送“创建我的数字人”开始。")
        persona = personas[0]
        published = tuple(
            str(item.get("content") or "") for item in persona.published_snapshots
        )
        sections = [
            ResponseSection(
                persona.display_name,
                items=(
                    f"状态：{persona.status.value}",
                    f"版本：r{persona.revision}",
                    f"已授权资料：{len(published)} 条",
                    "代答策略：" + {
                        "auto": "自动回答",
                        "risk_approval": "高风险问题需本人确认",
                        "owner_approval": "所有问题均需本人确认",
                    }.get(persona.response_mode, persona.response_mode),
                    "允许主题：" + ("、".join(persona.allowed_topics) or "仅按授权资料回答"),
                    "确认主题：" + (
                        "、".join(persona.approval_topics) or "使用企业高风险默认规则"
                    ),
                    "禁止主题：" + ("、".join(persona.denied_topics) or "未设置"),
                ),
            )
        ]
        if published:
            sections.append(ResponseSection("已发布资料", items=published[:5]))
        actions: list[ResponseAction] = []
        if persona.status is DigitalPersonaStatus.ACTIVE:
            actions.append(self._action(post, "persona_archive", persona.ref, "暂停", "danger"))
        else:
            included = set(persona.source_memory_ids)
            available = self.memories.list_active(
                installation_id=scope.installation_id,
                tenant_id=scope.tenant_id,
                owner_id=scope.owner_id,
                limit=20,
            )
            for item in (item for item in available if item.id not in included):
                actions.append(self._action(
                    post, "persona_add", f"{persona.ref}|{item.ref}",
                    f"授权：{item.content[:12]}", "default",
                ))
                if len(actions) >= 3:
                    break
            if persona.published_snapshots:
                actions.append(self._action(
                    post, "persona_publish", persona.ref, "发布数字人", "success"
                ))
        return ResponseView(
            kind=ResponseKind.STATUS,
            title="我的数字人",
            summary="只有下方明确授权的资料会被复制为对外快照。",
            status=RunStatus.SUCCEEDED,
            sections=tuple(sections),
            actions=tuple(actions[:5]),
        )

    def handle_action(
        self, claims: ActionClaims, *, actor_id: str, post: dict, scope: Scope
    ) -> str:
        if scope.kind is not ScopeKind.PERSONAL or actor_id != claims.requested_by:
            raise PermissionError("persona action belongs to another owner")
        return self._execute(
            claims.action, claims.target, actor_id=actor_id, post=post, scope=scope
        )

    def _execute(
        self, action: str, target: str, *, actor_id: str, post: dict, scope: Scope
    ) -> str:
        if action == "persona_add":
            persona_ref, memory_ref = target.split("|", 1)
            current = self.personas.get(persona_ref, owner_id=actor_id)
            revised = self.personas.create_revision(
                installation_id=current.installation_id,
                tenant_id=current.tenant_id,
                owner_id=current.owner_id,
                owner_username=current.owner_username,
                scope_id=current.scope_id,
                display_name=current.display_name,
                allowed_topics=current.allowed_topics,
                approval_topics=current.approval_topics,
                denied_topics=current.denied_topics,
                response_mode=current.response_mode,
                source_memory_ids=(*current.source_memory_ids, memory_ref),
                persona_id=current.id,
            )
            self._audit("persona.revision", post, scope, revised.ref, "source_added")
            return "资料已授权并生成新草稿版本。发送“管理我的数字人”可继续或发布。"
        if action == "persona_publish":
            persona = self.personas.activate(target, owner_id=actor_id)
            self._audit("persona.revision", post, scope, persona.ref, "published")
            return f"{persona.display_name} 已发布，可以接受其他人的提问。"
        if action == "persona_archive":
            persona = self.personas.archive(target, owner_id=actor_id)
            self._audit("persona.revision", post, scope, persona.ref, "archived")
            return f"{persona.display_name} 已暂停。"
        raise ValueError("unsupported persona action")

    def create_reply_request(
        self,
        invocation: PersonaInvocation,
        post: dict,
        *,
        requester_username: str,
        draft_text: str,
        status_post_id: str,
    ) -> PersonaReplyRequest:
        if self.reply_requests is None or not invocation.approval_required:
            raise RuntimeError("persona reply approval is not configured")
        persona = self.personas.get(invocation.persona_ref)
        return self.reply_requests.create(
            installation_id=persona.installation_id,
            tenant_id=persona.tenant_id,
            persona_ref=persona.ref,
            persona_hash=persona.sha256,
            owner_id=persona.owner_id,
            requester_id=str(post.get("user_id") or ""),
            requester_username=requester_username,
            source_scope_id=str(post.get("_scope_id") or ""),
            source_channel_id=str(post.get("channel_id") or ""),
            source_root_id=str(post.get("root_id") or post.get("id") or ""),
            source_status_post_id=status_post_id,
            question=invocation.question,
            draft_text=draft_text,
            approval_reason=invocation.approval_reason,
        )

    def approval_view(self, request: PersonaReplyRequest, post: dict) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.APPROVAL,
            title="数字人代答待确认",
            summary=request.approval_reason,
            status=RunStatus.WAITING_APPROVAL,
            sections=(
                ResponseSection("提问者", f"@{request.requester_username}"),
                ResponseSection("问题", request.question),
                ResponseSection("回答草稿", request.draft_text),
            ),
            actions=self._reply_actions(request, post),
        )

    def waiting_view(self, request: PersonaReplyRequest) -> ResponseView:
        persona = self.personas.get(request.persona_ref)
        return ResponseView(
            kind=ResponseKind.APPROVAL,
            title=f"{persona.display_name} · 等待本人确认",
            summary="回答草稿已发送给数字人所有者，确认后会回复当前 Thread。",
            status=RunStatus.WAITING_APPROVAL,
            run_id=f"persona-reply:{request.id}",
        )

    def decide_reply(
        self,
        request_id: str,
        *,
        actor_id: str,
        approved: bool,
        draft_text: str = "",
    ) -> PersonaReplyRequest:
        if self.reply_requests is None:
            raise RuntimeError("persona reply approval is not configured")
        self.pending_reply(request_id, actor_id=actor_id)
        return self.reply_requests.decide(
            request_id,
            actor_id=actor_id,
            approved=approved,
            draft_text=draft_text,
        )

    def pending_reply(self, request_id: str, *, actor_id: str) -> PersonaReplyRequest:
        if self.reply_requests is None:
            raise RuntimeError("persona reply approval is not configured")
        request = self.reply_requests.get(request_id)
        if request.owner_id != actor_id:
            raise PermissionError("only the persona owner can decide this reply")
        if request.state is not PersonaReplyState.PENDING:
            raise ValueError("persona reply request was already decided")
        if request.expires_at < time.time():
            raise ValueError("persona reply request has expired")
        persona = self.personas.get(request.persona_ref, owner_id=actor_id)
        if (
            persona.status is not DigitalPersonaStatus.ACTIVE
            or persona.sha256 != request.persona_hash
        ):
            raise PermissionError("persona version is no longer active")
        return request

    @staticmethod
    def reply_command(message: str) -> tuple[str, str, str] | None:
        parts = message.strip().split(maxsplit=2)
        if len(parts) == 2 and parts[0] in {"同意代答", "拒绝代答"}:
            return ("approve" if parts[0] == "同意代答" else "reject", parts[1], "")
        if len(parts) == 3 and parts[0] == "修改代答":
            return "approve", parts[1], parts[2].strip()
        return None

    def final_view(self, request: PersonaReplyRequest) -> ResponseView:
        persona = self.personas.get(request.persona_ref)
        return ResponseView(
            kind=ResponseKind.RESULT,
            title=f"{persona.display_name} · 数字人代理",
            summary=request.draft_text,
            status=RunStatus.SUCCEEDED,
            sections=(
                ResponseSection(
                    "代理声明",
                    items=(
                        "由 MMAG 根据所有者明确发布的资料快照生成，并经本人确认，并非本人实时发言。",
                        f"数字人版本：`{request.persona_ref}`",
                    ),
                ),
            ),
            run_id=f"persona-reply:{request.id}",
        )

    @staticmethod
    def rejected_view(request: PersonaReplyRequest) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.ERROR,
            title="数字人所有者未授权发送",
            summary="该回答草稿已被数字人所有者拒绝，不会对外发送。",
            status=RunStatus.FAILED,
            run_id=f"persona-reply:{request.id}",
        )

    def _reply_actions(
        self, request: PersonaReplyRequest, post: dict
    ) -> tuple[ResponseAction, ...]:
        if self.action_tokens is None:
            return ()
        shared = {
            "target": request.id,
            "scope_id": str(post["_scope_id"]),
            "run_id": f"persona-reply:{request.id}",
            "conversation_id": str(post["channel_id"]),
            "root_id": request.id,
            "requested_by": request.owner_id,
        }
        labels = (
            ("persona_reply_approve", "同意发送", "success"),
            ("persona_reply_edit", "修改后发送", "primary"),
            ("persona_reply_reject", "拒绝", "danger"),
        )
        return tuple(
            ResponseAction(
                action,
                label,
                action,
                request.id,
                style=style,
                fallback=(
                    f"`同意代答 {request.id}`"
                    if action.endswith("approve")
                    else f"`拒绝代答 {request.id}`"
                    if action.endswith("reject")
                    else f"`修改代答 {request.id} 新的回答内容`"
                ),
                token=self.action_tokens.issue(action=action, **shared),
            )
            for action, label, style in labels
        )

    def _action(
        self, post: dict, action: str, target: str, label: str, style: str
    ) -> ResponseAction:
        command = {
            "persona_add": "授权资料", "persona_publish": "发布数字人",
            "persona_archive": "暂停数字人",
        }[action]
        token = ""
        if self.action_tokens is not None:
            token = self.action_tokens.issue(
                action=action,
                target=target,
                scope_id=str(post["_scope_id"]),
                run_id=f"mattermost:{post.get('id', '')}",
                conversation_id=str(post["channel_id"]),
                root_id=str(post.get("root_id") or post.get("id") or ""),
                requested_by=str(post["user_id"]),
            )
        return ResponseAction(
            action, label, action, target, style=style,
            fallback=f"`{command} {target}`", token=token,
        )

    def _audit(
        self, event_type: str, post: dict, scope: Scope, target: str, decision: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if self.audit_store is None:
            return
        self.audit_store.append_audit(
            event_type, actor_id=scope.owner_id, scope_id=scope.id,
            trace_id=f"mattermost:{post.get('id', '')}", target=target,
            decision=decision, details={"schema_version": "1.0", **(details or {})},
        )

    @staticmethod
    def _topics(
        message: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        def capture(label: str) -> tuple[str, ...]:
            match = re.search(rf"{label}(.+?)(?:[，,。；;]|$)", message)
            if match is None:
                return ()
            return tuple(
                item.strip()[:80]
                for item in re.split(r"[、/]|和", match.group(1))
                if item.strip()
            )[:20]

        return (
            capture("允许回答"),
            capture("(?:需要确认|确认后回答)"),
            capture("(?:不要回答|禁止回答)"),
        )

    @staticmethod
    def _status(title: str, summary: str) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.STATUS, title=title, summary=summary,
            status=RunStatus.SUCCEEDED,
        )

    @staticmethod
    def _error(title: str, summary: str) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.ERROR, title=title, summary=summary,
            status=RunStatus.FAILED,
        )
