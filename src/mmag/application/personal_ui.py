"""Mattermost use cases for Personal Skills and WorkCases."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from ..control_plane import InteractionStatus, ScopeKind, WorkCaseStatus
from .personal_drafts import PersonalSkillDraftBuilder
from .personal_intents import IntentDecision, WorkspaceIntent, WorkspaceIntentResolver
from .views import (
    ResponseAction,
    ResponseKind,
    ResponseSection,
    ResponseView,
    RunStatus,
)

if TYPE_CHECKING:
    from ..agent_system import AgentRequest
    from ..control_plane import (
        InteractionSessionStore,
        PersonalSkill,
        PersonalSkillStore,
        Scope,
        WorkCaseStore,
    )
    from .actions import ActionClaims, ActionTokenService


class PersonalWorkspaceUI:
    """Keep personal workflow management outside Agent execution prompts."""

    def __init__(
        self, *, personal_skills: PersonalSkillStore, work_cases: WorkCaseStore,
        interactions: InteractionSessionStore, action_tokens: ActionTokenService | None,
        audit_store=None, intent_runtime=None,
    ) -> None:
        self.personal_skills = personal_skills
        self.work_cases = work_cases
        self.interactions = interactions
        self.action_tokens = action_tokens
        self.audit_store = audit_store
        self.drafts = PersonalSkillDraftBuilder(personal_skills)
        self.intents = WorkspaceIntentResolver(intent_runtime)

    async def consume_message(
        self, post: dict, message: str, scope: Scope,
    ) -> tuple[bool, ResponseView | None, str]:
        if scope.kind is not ScopeKind.PERSONAL:
            return False, None, ""
        normalized = re.sub(r"\s+", "", message).lower()
        if normalized in {"我的skills", "skills", "myskills"}:
            cancelled = self._cancel_session(scope)
            view = self.skills_view(post, scope)
            if cancelled:
                view = replace(view, summary=f"{view.summary} 已取消上一个待输入操作。")
            return True, view, ""
        if normalized in {"我的案例", "workcases", "cases"}:
            cancelled = self._cancel_session(scope)
            view = self.cases_view(post, scope)
            if cancelled:
                view = replace(view, summary=f"{view.summary} 已取消上一个待输入操作。")
            return True, view, ""
        command = self._text_command(message)
        if command is not None:
            action, target = command
            try:
                result = self._execute(
                    action, target, actor_id=scope.owner_id, post=post, scope=scope
                )
            except (KeyError, PermissionError, ValueError) as error:
                return True, self._error("操作未完成", str(error)), ""
            return True, self._status("操作已受理", result), ""
        session = self.interactions.get_open(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id, conversation_id=scope.conversation_id,
        )
        if session is None:
            decision = await self.intents.resolve(
                message,
                skills=tuple(self.personal_skills.list_latest(
                    installation_id=scope.installation_id,
                    tenant_id=scope.tenant_id,
                    owner_id=scope.owner_id,
                )),
                scope=scope,
                trace_id=f"mattermost:{post.get('id', '')}",
            )
            return self._handle_intent(decision, post=post, scope=scope)
        if normalized in {"取消", "cancel"}:
            self.interactions.complete(session.id, status=InteractionStatus.CANCELLED)
            return True, self._status("已取消", "本次操作已取消。"), ""
        if session.kind == "run_personal_skill":
            self.interactions.complete(session.id)
            return False, None, str(session.payload["personal_skill_ref"])
        if session.kind == "edit_personal_skill":
            current = self.personal_skills.get(
                str(session.payload["personal_skill_ref"]), owner_id=scope.owner_id
            )
            revised = self._revise(current, instruction=message)
            self.interactions.complete(session.id)
            self._audit("personal_skill.revision", post, scope, revised.ref, "created",
                        {"previous_ref": current.ref})
            return True, self._draft_view(post, revised), ""
        return False, None, ""

    def _handle_intent(
        self, decision: IntentDecision, *, post: dict, scope: Scope
    ) -> tuple[bool, ResponseView | None, str]:
        if decision.intent is WorkspaceIntent.ORDINARY:
            return False, None, ""
        if decision.intent is WorkspaceIntent.SKILLS_LIST:
            return True, self.skills_view(post, scope), ""
        if decision.intent is WorkspaceIntent.CASES_LIST:
            return True, self.cases_view(post, scope), ""
        action = {
            WorkspaceIntent.SKILL_RUN: "pskill_run",
            WorkspaceIntent.SKILL_EDIT: "pskill_edit",
            WorkspaceIntent.SKILL_ACTIVATE: "pskill_activate",
            WorkspaceIntent.SKILL_ARCHIVE: "pskill_archive",
            WorkspaceIntent.SKILL_VERSIONS: "pskill_versions",
        }.get(decision.intent)
        if action is None or not decision.target_ref:
            return True, self._status(
                "需要确认", "我没有找到唯一匹配的个人 Skill，请从「我的 Skills」中选择。"
            ), ""
        if action in {"pskill_activate", "pskill_archive"}:
            label = "确认启用" if action == "pskill_activate" else "确认停用"
            return True, ResponseView(
                kind=ResponseKind.STATUS,
                title="请确认操作",
                summary=f"即将{label.removeprefix('确认')}个人 Skill `{decision.target_ref}`。",
                status=RunStatus.SUCCEEDED,
                actions=(self._action(post, action, decision.target_ref, label,
                                      "success" if action == "pskill_activate" else "danger"),),
            ), ""
        if action == "pskill_run" and not decision.needs_input:
            skill = self.personal_skills.get(decision.target_ref, owner_id=scope.owner_id)
            if skill.status.value != "active":
                return True, self._error("无法运行", "请先启用这个 Skill。"), ""
            return False, None, decision.target_ref
        try:
            result = self._execute(
                action, decision.target_ref, actor_id=scope.owner_id, post=post, scope=scope
            )
        except (KeyError, PermissionError, ValueError) as error:
            return True, self._error("操作未完成", str(error)), ""
        return True, self._status("操作已受理", result), ""

    def skills_view(self, post: dict, scope: Scope) -> ResponseView:
        skills = self.personal_skills.list_latest(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
        )
        if not skills:
            return self._status(
                "我的 Skills", "还没有个人 Skill。任务完成后点击「沉淀为 Skill」即可生成草稿。"
            )
        sections = tuple(
            ResponseSection(
                skill.name,
                items=(f"状态：{skill.status.value}", f"引用：`{skill.ref}`",
                       f"基础：`{skill.base_skill_ref}`", f"版本：r{skill.revision}"),
            )
            for skill in skills[:10]
        )
        actions: list[ResponseAction] = []
        for skill in skills[:1]:
            if skill.status.value == "active":
                actions.extend((self._action(post, "pskill_run", skill.ref, "运行"),
                                self._action(post, "pskill_archive", skill.ref, "停用", "danger")))
            else:
                actions.append(self._action(post, "pskill_activate", skill.ref, "启用", "success"))
            actions.append(self._action(post, "pskill_edit", skill.ref, "编辑"))
            actions.append(self._action(post, "pskill_versions", skill.ref, "版本"))
        return ResponseView(
            kind=ResponseKind.STATUS, title="我的 Skills", summary=f"共 {len(skills)} 个个人 Skill。",
            status=RunStatus.SUCCEEDED, sections=sections, actions=tuple(actions[:5]),
        )

    def cases_view(self, post: dict, scope: Scope) -> ResponseView:
        cases = self.work_cases.list_saved(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
        )
        sections = tuple(
            ResponseSection(case.goal[:60], items=(f"案例：`{case.id}`", f"反馈：{case.feedback or '未评价'}"))
            for case in cases[:10]
        )
        actions = list(
            self._action(post, "case_draft", case.id, "生成 Skill")
            for case in cases[:5] if case.skill_ref
        )
        grouped = tuple(case for case in cases if case.skill_ref == cases[0].skill_ref) if cases else ()
        if len(grouped) >= 2:
            target = ",".join(case.id for case in grouped[:5])
            actions.insert(0, self._action(post, "case_draft", target, "合并生成", "primary"))
        return ResponseView(
            kind=ResponseKind.STATUS, title="我的案例",
            summary=f"已保存 {len(cases)} 个案例。" if cases else "还没有保存的案例。",
            status=RunStatus.SUCCEEDED, sections=sections, actions=tuple(actions[:5]),
        )

    def attach_work_case(
        self, post: dict, scope: Scope, request: AgentRequest, agent_name: str,
        view: ResponseView, *, provenance: dict | None = None,
    ) -> ResponseView:
        if scope.kind is not ScopeKind.PERSONAL or view.status is not RunStatus.SUCCEEDED:
            return view
        skill_ref = request.skill.ref if request.skill is not None else ""
        personal_ref = request.skill.personal_ref if request.skill is not None else ""
        case = self.work_cases.create(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id, scope_id=scope.id, goal=request.prompt,
            result_summary=view.summary, agent_name=agent_name, skill_ref=skill_ref,
            personal_skill_ref=personal_ref, artifact_refs=(item.ref for item in view.artifacts),
            source_run_id=request.run_id,
            source_message_id=str(post.get("id") or ""),
            provenance=provenance or {},
        )
        self._audit("work_case.candidate", post, scope, case.id, "created",
                    {"agent_name": agent_name, "skill_ref": skill_ref})
        actions = [self._action(post, "case_save", case.id, "保存案例", "primary"),
                   self._action(post, "case_good", case.id, "有帮助", "success"),
                   self._action(post, "case_bad", case.id, "需改进", "warning")]
        if skill_ref:
            actions.append(self._action(post, "case_draft", case.id, "沉淀为 Skill"))
        return replace(view, actions=tuple((*view.actions, *actions)[:5]))

    def handle_action(self, claims: ActionClaims, *, actor_id: str, post: dict, scope: Scope) -> str:
        if scope.kind is not ScopeKind.PERSONAL or actor_id != claims.requested_by:
            raise PermissionError("personal action belongs to another owner")
        return self._execute(claims.action, claims.target, actor_id=actor_id, post=post, scope=scope)

    def _execute(self, action: str, target: str, *, actor_id: str, post: dict, scope: Scope) -> str:
        if action.startswith("pskill_"):
            skill = self.personal_skills.get(target, owner_id=actor_id)
            if action == "pskill_versions":
                revisions = self.personal_skills.list_revisions(skill.id, owner_id=actor_id)
                return "版本记录：" + "、".join(
                    f"r{item.revision}（{item.status.value}）" for item in revisions
                )
            if action == "pskill_activate":
                self.personal_skills.activate(skill.ref, owner_id=actor_id)
                self._audit("personal_skill.action", post, scope, skill.ref, "activated")
                return "Skill 已启用。发送「我的 Skills」可查看和运行。"
            if action == "pskill_archive":
                self.personal_skills.archive(skill.ref, owner_id=actor_id)
                self._audit("personal_skill.action", post, scope, skill.ref, "archived")
                return "Skill 已停用。"
            if action not in {"pskill_run", "pskill_edit"}:
                raise ValueError("unsupported Personal Skill action")
            if action == "pskill_run" and skill.status.value != "active":
                raise ValueError("请先启用这个 Skill，再运行。")
            kind = "run_personal_skill" if action == "pskill_run" else "edit_personal_skill"
            self._open(scope, kind, {"personal_skill_ref": skill.ref})
            self._audit("personal_skill.action", post, scope, skill.ref, action)
            return "请发送要处理的任务。" if action == "pskill_run" else "请发送新的执行要求；发送「取消」可退出。"
        case = self.work_cases.get(target.split(",", 1)[0], owner_id=actor_id)
        if action == "case_save":
            saved = self.work_cases.update(case.id, owner_id=actor_id, status=WorkCaseStatus.SAVED)
            self._audit("work_case.action", post, scope, case.id, "saved")
            similar = self.work_cases.similar_saved(saved)
            suffix = " 已有 3 个同类案例，可从「我的案例」合并生成 Skill。" if len(similar) >= 3 else ""
            return "案例已保存。" + suffix
        if action in {"case_good", "case_bad"}:
            feedback = "helpful" if action == "case_good" else "needs_improvement"
            self.work_cases.update(case.id, owner_id=actor_id, status=WorkCaseStatus.SAVED,
                                   feedback=feedback)
            self._audit("work_case.action", post, scope, case.id, feedback)
            return "反馈已记录，并已保存为案例。"
        if action == "case_draft":
            ids = tuple(dict.fromkeys(target.split(",")))
            cases = tuple(self.work_cases.get(item, owner_id=actor_id) for item in ids)
            for item in cases:
                self.work_cases.update(
                    item.id, owner_id=actor_id, status=WorkCaseStatus.SAVED
                )
            refreshed = tuple(self.work_cases.get(item, owner_id=actor_id) for item in ids)
            draft = self.drafts.build(scope, refreshed)
            self._audit("personal_skill.draft", post, scope, draft.ref, "created",
                        {"work_case_ids": list(ids), "base_skill_ref": draft.base_skill_ref})
            return f"个人 Skill 草稿「{draft.name}」已生成。发送「我的 Skills」审阅并启用。"
        raise ValueError("unsupported personal action")

    def _revise(self, current: PersonalSkill, *, instruction: str) -> PersonalSkill:
        return self.personal_skills.create_revision(
            installation_id=current.installation_id, tenant_id=current.tenant_id,
            owner_id=current.owner_id, scope_id=current.scope_id, name=current.name,
            description=current.description, base_skill_ref=current.base_skill_ref,
            preferred_agent=current.preferred_agent,
            activation_intents=current.activation_intents,
            activation_keywords=current.activation_keywords, auto_select=current.auto_select,
            instruction=instruction, template=current.template, skill_id=current.id,
            source_case_ids=current.source_case_ids,
        )

    def _draft_view(self, post: dict, skill: PersonalSkill) -> ResponseView:
        return ResponseView(
            kind=ResponseKind.STATUS, title="Skill 草稿已更新", summary=skill.name,
            status=RunStatus.SUCCEEDED,
            sections=(ResponseSection("版本", f"r{skill.revision}"),
                      ResponseSection("执行要求", skill.instruction[:1_500])),
            actions=(self._action(post, "pskill_activate", skill.ref, "启用", "success"),),
        )

    def _open(self, scope: Scope, kind: str, payload: dict) -> None:
        self.interactions.open(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id, scope_id=scope.id,
            conversation_id=scope.conversation_id, kind=kind, payload=payload,
        )

    def _cancel_session(self, scope: Scope) -> bool:
        return bool(self.interactions.cancel_open(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id, conversation_id=scope.conversation_id,
        ))

    @staticmethod
    def _text_command(message: str) -> tuple[str, str] | None:
        parts = message.strip().split(maxsplit=1)
        if len(parts) != 2:
            return None
        commands = {
            "运行": "pskill_run", "run": "pskill_run",
            "启用": "pskill_activate", "activate": "pskill_activate",
            "停用": "pskill_archive", "archive": "pskill_archive",
            "编辑": "pskill_edit", "edit": "pskill_edit",
            "版本": "pskill_versions", "versions": "pskill_versions",
            "保存案例": "case_save", "案例有帮助": "case_good",
            "案例需改进": "case_bad", "生成skill": "case_draft",
        }
        action = commands.get(parts[0].lower())
        return (action, parts[1].strip()) if action and parts[1].strip() else None

    def _action(
        self, post: dict, action: str, target: str, label: str, style: str = "default",
    ) -> ResponseAction:
        token = ""
        command = {
            "pskill_run": "运行", "pskill_activate": "启用", "pskill_archive": "停用",
            "pskill_edit": "编辑", "pskill_versions": "版本", "case_save": "保存案例",
            "case_good": "案例有帮助", "case_bad": "案例需改进", "case_draft": "生成Skill",
        }[action]
        fallback = f"`{command} {target}`"
        if self.action_tokens is not None:
            token = self.action_tokens.issue(
                action=action, target=target, scope_id=str(post["_scope_id"]),
                run_id=f"mattermost:{post.get('id', '')}",
                conversation_id=str(post["channel_id"]),
                root_id=str(post.get("root_id") or post.get("id") or ""),
                requested_by=str(post["user_id"]),
            )
        return ResponseAction(action, label, action, target, style=style,
                              fallback=fallback, token=token)

    def _audit(
        self, event_type: str, post: dict, scope: Scope, target: str, decision: str,
        details: dict | None = None,
    ) -> None:
        if self.audit_store is None:
            return
        self.audit_store.append_audit(
            event_type, actor_id=scope.owner_id, scope_id=scope.id,
            trace_id=f"mattermost:{post.get('id', '')}", target=target,
            decision=decision, details={"schema_version": "1.0", **(details or {})},
        )

    @staticmethod
    def _status(title: str, summary: str) -> ResponseView:
        return ResponseView(kind=ResponseKind.STATUS, title=title, summary=summary,
                            status=RunStatus.SUCCEEDED)

    @staticmethod
    def _error(title: str, summary: str) -> ResponseView:
        return ResponseView(kind=ResponseKind.ERROR, title=title, summary=summary,
                            status=RunStatus.FAILED)
