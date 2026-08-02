"""Mattermost use cases for Personal Skills and WorkCases."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from ..control_plane import InteractionStatus, ScopeKind, WorkCaseStatus
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
        WorkCase,
        WorkCaseStore,
    )
    from .actions import ActionClaims, ActionTokenService


class PersonalWorkspaceUI:
    """Keep personal workflow management outside Agent execution prompts."""

    def __init__(
        self, *, personal_skills: PersonalSkillStore, work_cases: WorkCaseStore,
        interactions: InteractionSessionStore, action_tokens: ActionTokenService | None,
        audit_store=None,
    ) -> None:
        self.personal_skills = personal_skills
        self.work_cases = work_cases
        self.interactions = interactions
        self.action_tokens = action_tokens
        self.audit_store = audit_store

    def consume_message(
        self, post: dict, message: str, scope: Scope,
    ) -> tuple[bool, ResponseView | None, str]:
        if scope.kind is not ScopeKind.PERSONAL:
            return False, None, ""
        normalized = re.sub(r"\s+", "", message).lower()
        if normalized in {"我的skills", "skills", "myskills"}:
            return True, self.skills_view(post, scope), ""
        if normalized in {"我的案例", "workcases", "cases"}:
            return True, self.cases_view(post, scope), ""
        session = self.interactions.get_open(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id, conversation_id=scope.conversation_id,
        )
        if session is None:
            return False, None, ""
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
                items=(f"状态：{skill.status.value}", f"基础：`{skill.base_skill_ref}`",
                       f"版本：r{skill.revision}"),
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
        view: ResponseView,
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
        if claims.action.startswith("pskill_"):
            skill = self.personal_skills.get(claims.target, owner_id=actor_id)
            if claims.action == "pskill_versions":
                revisions = self.personal_skills.list_revisions(skill.id, owner_id=actor_id)
                return "版本记录：" + "、".join(
                    f"r{item.revision}（{item.status.value}）" for item in revisions
                )
            if claims.action == "pskill_activate":
                self.personal_skills.activate(skill.ref, owner_id=actor_id)
                self._audit("personal_skill.action", post, scope, skill.ref, "activated")
                return "Skill 已启用。发送「我的 Skills」可查看和运行。"
            if claims.action == "pskill_archive":
                self.personal_skills.archive(skill.ref, owner_id=actor_id)
                self._audit("personal_skill.action", post, scope, skill.ref, "archived")
                return "Skill 已停用。"
            kind = "run_personal_skill" if claims.action == "pskill_run" else "edit_personal_skill"
            self._open(scope, kind, {"personal_skill_ref": skill.ref})
            self._audit("personal_skill.action", post, scope, skill.ref, claims.action)
            return "请发送要处理的任务。" if claims.action == "pskill_run" else "请发送新的执行要求；发送「取消」可退出。"
        case = self.work_cases.get(claims.target.split(",", 1)[0], owner_id=actor_id)
        if claims.action == "case_save":
            saved = self.work_cases.update(case.id, owner_id=actor_id, status=WorkCaseStatus.SAVED)
            self._audit("work_case.action", post, scope, case.id, "saved")
            similar = self.work_cases.similar_saved(saved)
            suffix = " 已有 3 个同类案例，可从「我的案例」合并生成 Skill。" if len(similar) >= 3 else ""
            return "案例已保存。" + suffix
        if claims.action in {"case_good", "case_bad"}:
            feedback = "helpful" if claims.action == "case_good" else "needs_improvement"
            self.work_cases.update(case.id, owner_id=actor_id, status=WorkCaseStatus.SAVED,
                                   feedback=feedback)
            self._audit("work_case.action", post, scope, case.id, feedback)
            return "反馈已记录，并已保存为案例。"
        if claims.action == "case_draft":
            ids = tuple(dict.fromkeys(claims.target.split(",")))
            cases = tuple(self.work_cases.get(item, owner_id=actor_id) for item in ids)
            draft = self._build_draft(scope, cases)
            for item in cases:
                self.work_cases.update(
                    item.id, owner_id=actor_id, status=WorkCaseStatus.SAVED
                )
            self._audit("personal_skill.draft", post, scope, draft.ref, "created",
                        {"work_case_ids": list(ids), "base_skill_ref": draft.base_skill_ref})
            return f"个人 Skill 草稿「{draft.name}」已生成。发送「我的 Skills」审阅并启用。"
        raise ValueError("unsupported personal action")

    def _build_draft(self, scope: Scope, cases: tuple[WorkCase, ...]) -> PersonalSkill:
        base_ref = cases[0].skill_ref
        if not base_ref or any(case.skill_ref != base_ref for case in cases):
            raise ValueError("WorkCases must share one base Skill")
        goals = "\n".join(f"- {case.goal[:500]}" for case in cases)
        outcomes = "\n".join(f"- {case.result_summary[:800]}" for case in cases)
        keywords = tuple(dict.fromkeys(re.findall(r"[\w\u4e00-\u9fff]{2,12}", cases[0].goal)))[:8]
        return self.personal_skills.create_revision(
            installation_id=scope.installation_id, tenant_id=scope.tenant_id,
            owner_id=scope.owner_id, scope_id=scope.id,
            name=f"{cases[0].agent_name or '个人'}工作流",
            description=f"由 {len(cases)} 个已验证 WorkCase 生成的草稿。",
            base_skill_ref=base_ref, preferred_agent=cases[0].agent_name,
            activation_keywords=keywords, auto_select=False,
            instruction=("复用以下已验证任务的工作方法。先确认目标与输入，再按基础 Skill 执行；"
                         "保持结果结构一致，不复制案例中的事实数据。\n\n任务模式：\n"
                         f"{goals}\n\n期望结果模式：\n{outcomes}"),
        )

    def _revise(self, current: PersonalSkill, *, instruction: str) -> PersonalSkill:
        return self.personal_skills.create_revision(
            installation_id=current.installation_id, tenant_id=current.tenant_id,
            owner_id=current.owner_id, scope_id=current.scope_id, name=current.name,
            description=current.description, base_skill_ref=current.base_skill_ref,
            preferred_agent=current.preferred_agent,
            activation_intents=current.activation_intents,
            activation_keywords=current.activation_keywords, auto_select=current.auto_select,
            instruction=instruction, template=current.template, skill_id=current.id,
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

    def _action(
        self, post: dict, action: str, target: str, label: str, style: str = "default",
    ) -> ResponseAction:
        token = ""
        fallback = "发送「我的 Skills」管理。"
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
