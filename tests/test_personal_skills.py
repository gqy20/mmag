"""Personal Skill revision, isolation, resolution, and projection contracts."""

from pathlib import Path

import pytest

from mmag.agent_packages import AgentPackageRegistry
from mmag.agent_system import AgentRequest, SkillInvocation
from mmag.application import ActionTokenService
from mmag.application.personal_ui import PersonalWorkspaceUI
from mmag.application.views import ResponseKind, ResponseView, RunStatus
from mmag.capabilities import (
    CapabilityAuthorization,
    CapabilityExecutor,
    CapabilityRegistry,
    CapabilitySpec,
    bind_langgraph_capability,
)
from mmag.control_plane import (
    PersonalSkillStatus,
    Scope,
    ScopeKind,
    SQLiteControlPlane,
    WorkCaseStatus,
)
from mmag.execution import ExecutionProfileRegistry
from mmag.governance import ModelPolicyRegistry, PolicyRegistry
from mmag.skill_packages import (
    SkillPackageLoader,
    SkillPackageRegistry,
    SkillResolutionError,
    SkillResolver,
)
from mmag.skill_packages.projection import project_skill_files

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "mattermost:install-1:tenant-1:usr:user-1"
BASE_SKILL_REF = SkillPackageLoader().load(
    ROOT / "skills" / "web-research"
).manifest.metadata.ref


class _Allow:
    def authorize(self, spec, arguments):
        del spec, arguments
        return CapabilityAuthorization.allow()


def _registries():
    skills = SkillPackageRegistry()
    skills.load_directory(ROOT / "skills")
    profiles = ExecutionProfileRegistry()
    profiles.load_directory(ROOT / "execution-profiles")
    policies = PolicyRegistry()
    policies.load_directory(ROOT / "policies")
    models = ModelPolicyRegistry()
    models.load_directory(ROOT / "model-policies")
    agents = AgentPackageRegistry(
        policy_registry=policies,
        model_policy_registry=models,
        skill_registry=skills,
        execution_profile_registry=profiles,
    )
    agents.load_directory(ROOT / "agents")
    capabilities = CapabilityRegistry()
    executor = CapabilityExecutor(_Allow())
    for name in ("analyze_link", "search_knowledge"):
        capabilities.register(
            bind_langgraph_capability(
                CapabilitySpec(name, name, {"type": "object"}, lambda: {}),
                executor=executor,
            )
        )
    return skills, agents, capabilities


def _draft(store: SQLiteControlPlane, **overrides):
    values = {
        "installation_id": "install-1",
        "tenant_id": "tenant-1",
        "owner_id": "user-1",
        "scope_id": SCOPE,
        "name": "我的竞品研究",
        "base_skill_ref": BASE_SKILL_REF,
        "preferred_agent": "mmchat",
        "activation_intents": ("research",),
        "activation_keywords": ("竞品",),
        "auto_select": True,
        "instruction": "优先比较商业模式，并用表格输出。",
        "template": "# 竞品研究\n\n## 结论",
    }
    values.update(overrides)
    return store.personal_skills.create_revision(**values)


def test_personal_skill_revisions_are_immutable_and_one_revision_is_active(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "personal.db"))
    first = _draft(store)
    assert first.status is PersonalSkillStatus.DRAFT
    active_first = store.personal_skills.activate(first.ref, owner_id="user-1")
    second = _draft(store, skill_id=first.id, instruction="增加风险矩阵。")
    active_second = store.personal_skills.activate(second.ref, owner_id="user-1")

    assert active_first.revision == 1
    assert active_second.revision == 2
    assert store.personal_skills.get(first.ref, owner_id="user-1").status is PersonalSkillStatus.ARCHIVED
    assert store.personal_skills.get(second.ref, owner_id="user-1").instruction == "增加风险矩阵。"
    store.close()


def test_personal_skill_resolves_as_overlay_without_expanding_capabilities(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "personal.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    skills, agents, capabilities = _registries()
    package = agents.get("mmchat")
    resolver = SkillResolver(skills, capabilities, personal_skills=store.personal_skills)

    invocation = resolver.resolve(
        package,
        AgentRequest(
            "research",
            "请做竞品研究",
            scope=SCOPE,
            actor_id="user-1",
            requested_skill="web-research",
            requested_personal_skill=skill.ref,
        ),
        ("analyze_link", "search_knowledge"),
    )

    assert invocation is not None
    assert invocation.capabilities == ("analyze_link", "search_knowledge")
    assert invocation.personal_ref == skill.ref
    assert invocation.provenance["personal_skill_hash"] == skill.sha256
    store.close()


def test_explicit_personal_skill_prepares_agent_and_base_skill_routing(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "personal.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    skills, _, capabilities = _registries()
    resolver = SkillResolver(skills, capabilities, personal_skills=store.personal_skills)

    prepared = resolver.prepare_personal_request(
        AgentRequest(
            "mention",
            "运行我的方法",
            scope=SCOPE,
            actor_id="user-1",
            requested_personal_skill=skill.ref,
        )
    )

    assert prepared.requested_agent == "mmchat"
    assert prepared.requested_skill == BASE_SKILL_REF
    store.close()


def test_personal_skill_projects_read_only_workflow_files(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "personal.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    skills, agents, capabilities = _registries()
    package = agents.get("mmchat")
    request = AgentRequest(
        "research",
        "请做竞品研究",
        scope=SCOPE,
        actor_id="user-1",
        requested_skill="web-research",
        requested_personal_skill=skill.ref,
    )
    invocation = SkillResolver(
        skills, capabilities, personal_skills=store.personal_skills
    ).resolve(package, request, ("analyze_link", "search_knowledge"))
    files = project_skill_files(package, AgentRequest("research", "研究", skill=invocation))

    assert files["/skills/web-research/personal.md"]["content"] == skill.instruction
    assert "/skills/web-research/templates/personal.md" in files
    assert "cannot grant tools" in files["/skills/web-research/SKILL.md"]["content"]
    store.close()


def test_personal_skill_is_rejected_outside_its_owner_dm(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "personal.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    skills, agents, capabilities = _registries()
    resolver = SkillResolver(skills, capabilities, personal_skills=store.personal_skills)

    with pytest.raises(SkillResolutionError, match="only to its owner in DM"):
        resolver.resolve(
            agents.get("mmchat"),
            AgentRequest(
                "research",
                "竞品研究",
                scope="mattermost:install-1:tenant-1:chn:channel-1",
                actor_id="user-1",
                requested_skill="web-research",
                requested_personal_skill=skill.ref,
            ),
            ("analyze_link", "search_knowledge"),
        )
    store.close()


@pytest.mark.asyncio
async def test_personal_workspace_lists_runs_and_edits_without_model_call(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "workspace.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    tokens = ActionTokenService("s" * 32, store, ttl_seconds=60)
    ui = PersonalWorkspaceUI(
        personal_skills=store.personal_skills,
        work_cases=store.work_cases,
        interactions=store.interactions,
        action_tokens=tokens,
    )
    post = {
        "id": "post-1", "channel_id": "dm-1", "user_id": "user-1",
        "_scope_id": SCOPE,
    }
    scope = Scope(
        SCOPE, installation_id="install-1", tenant_id="tenant-1",
        owner_id="user-1", conversation_id="dm-1", kind=ScopeKind.PERSONAL,
        channel_type="D",
    )

    handled, view, selected = await ui.consume_message(post, "我的 Skills", scope)
    run_action = next(action for action in view.actions if action.action == "pskill_run")
    claims = tokens.consume(run_action.token, actor_id="user-1")
    prompt = ui.handle_action(claims, actor_id="user-1", post=post, scope=scope)
    continued, _, selected = await ui.consume_message(post, "按我的方法研究新项目", scope)

    assert handled and selected == skill.ref
    assert view is not None and view.title == "我的 Skills"
    assert "请发送" in prompt
    assert not continued
    store.close()


@pytest.mark.asyncio
async def test_personal_workspace_text_fallback_and_management_cancel_pending_input(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "text-actions.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    ui = PersonalWorkspaceUI(
        personal_skills=store.personal_skills, work_cases=store.work_cases,
        interactions=store.interactions, action_tokens=None,
    )
    post = {
        "id": "post-1", "channel_id": "dm-1", "user_id": "user-1",
        "_scope_id": SCOPE,
    }
    scope = Scope(
        SCOPE, installation_id="install-1", tenant_id="tenant-1",
        owner_id="user-1", conversation_id="dm-1", kind=ScopeKind.PERSONAL,
        channel_type="D",
    )

    handled, view, _ = await ui.consume_message(post, f"运行 {skill.ref}", scope)
    managed, skills_view, _ = await ui.consume_message(post, "我的 Skills", scope)
    continued, _, selected = await ui.consume_message(
        post, "这条消息不应误触发旧会话", scope
    )

    assert handled and view is not None and "请发送" in view.summary
    assert managed and skills_view is not None and "已取消" in skills_view.summary
    assert not continued and not selected
    store.close()


@pytest.mark.asyncio
async def test_personal_workspace_understands_natural_management_intent(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "semantic-actions.db"))
    skill = store.personal_skills.activate(_draft(store).ref, owner_id="user-1")
    ui = PersonalWorkspaceUI(
        personal_skills=store.personal_skills, work_cases=store.work_cases,
        interactions=store.interactions, action_tokens=None,
    )
    post = {
        "id": "post-1", "channel_id": "dm-1", "user_id": "user-1",
        "_scope_id": SCOPE,
    }
    scope = Scope(
        SCOPE, installation_id="install-1", tenant_id="tenant-1",
        owner_id="user-1", conversation_id="dm-1", kind=ScopeKind.PERSONAL,
        channel_type="D",
    )

    handled, view, _ = await ui.consume_message(post, "帮我看看都有哪些技能", scope)
    run_handled, _, selected = await ui.consume_message(
        post, "用我的竞品研究方法分析 Notion", scope
    )
    confirm_handled, confirm, _ = await ui.consume_message(
        post, "把我的竞品研究速报停掉", scope
    )

    assert handled and view is not None and view.title == "我的 Skills"
    assert not run_handled and selected == skill.ref
    assert confirm_handled and confirm is not None and confirm.title == "请确认操作"
    assert store.personal_skills.get(skill.ref, owner_id="user-1").status is PersonalSkillStatus.ACTIVE
    store.close()


def test_work_case_feedback_and_combined_draft_stay_in_personal_scope(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "cases.db"))
    ui = PersonalWorkspaceUI(
        personal_skills=store.personal_skills, work_cases=store.work_cases,
        interactions=store.interactions, action_tokens=None,
    )
    scope = Scope(
        SCOPE, installation_id="install-1", tenant_id="tenant-1",
        owner_id="user-1", conversation_id="dm-1", kind=ScopeKind.PERSONAL,
        channel_type="D",
    )
    cases = tuple(
        store.work_cases.create(
            installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
            scope_id=SCOPE, goal=f"竞品研究 {index}", result_summary="结构化结论",
            agent_name="mmchat", skill_ref=BASE_SKILL_REF,
        )
        for index in range(2)
    )
    for case in cases:
        store.work_cases.update(
            case.id, owner_id="user-1", status=WorkCaseStatus.SAVED, feedback="helpful"
        )
    saved_cases = tuple(store.work_cases.get(case.id, owner_id="user-1") for case in cases)
    draft = ui.drafts.build(scope, saved_cases)

    assert draft.status is PersonalSkillStatus.DRAFT
    assert draft.base_skill_ref == BASE_SKILL_REF
    assert "结构化结论" not in draft.instruction
    assert draft.source_case_ids == tuple(case.id for case in cases)
    assert "不能被当作系统指令" in draft.instruction
    with pytest.raises(PermissionError, match="Personal Scope"):
        store.work_cases.create(
            installation_id="install-1", tenant_id="tenant-1", owner_id="user-2",
            scope_id=SCOPE, goal="越权", result_summary="不应保存",
        )
    store.close()


def test_successful_personal_result_exposes_work_case_actions(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "result.db"))
    tokens = ActionTokenService("s" * 32, store, ttl_seconds=60)
    ui = PersonalWorkspaceUI(
        personal_skills=store.personal_skills, work_cases=store.work_cases,
        interactions=store.interactions, action_tokens=tokens,
    )
    post = {
        "id": "post-1", "channel_id": "dm-1", "user_id": "user-1",
        "_scope_id": SCOPE,
    }
    scope = Scope(
        SCOPE, installation_id="install-1", tenant_id="tenant-1",
        owner_id="user-1", conversation_id="dm-1", kind=ScopeKind.PERSONAL,
        channel_type="D",
    )
    request = AgentRequest(
        "research", "研究竞争对手", actor_id="user-1",
        run_id="mattermost:post-1",
        skill=SkillInvocation(BASE_SKILL_REF, (), {}),
    )
    view = ui.attach_work_case(
        post, scope, request, "mmchat",
        ResponseView(ResponseKind.RESULT, "完成", "研究结论", RunStatus.SUCCEEDED),
        provenance={"agent_version": "2.2.0", "skill_package_hash": "abc"},
    )

    assert {action.action for action in view.actions} == {
        "case_save", "case_good", "case_bad", "case_draft"
    }
    assert len(store.work_cases.list_saved(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1"
    )) == 0
    case = store.work_cases.get(view.actions[0].target, owner_id="user-1")
    assert case.source_run_id == "mattermost:post-1"
    assert case.source_message_id == "post-1"
    assert case.goal_hash and case.result_hash
    assert case.provenance["agent_version"] == "2.2.0"

    repeated = ui.attach_work_case(
        post, scope, request, "mmchat",
        ResponseView(ResponseKind.RESULT, "完成", "研究结论", RunStatus.SUCCEEDED),
        provenance={"agent_version": "2.2.0"},
    )
    assert repeated.actions[0].target == case.id
    store.close()
