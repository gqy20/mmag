"""Digital Persona publication, isolation, and Mattermost interaction contracts."""

import pytest

from mmag.agent_packages.models import PromptAsset
from mmag.application import ActionTokenService, BotIdentity, ContextBuilder
from mmag.application.persona_ui import PersonaWorkspaceUI
from mmag.control_plane import (
    DigitalPersonaStatus,
    MattermostScopeResolver,
    MemoryItemKind,
    PersonaReplyState,
    Scope,
    ScopeKind,
    SQLiteControlPlane,
)
from mmag.memory import Memory

SCOPE = "mattermost:install-1:tenant-1:usr:user-1"


def _scope() -> Scope:
    return Scope(
        SCOPE, installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
        conversation_id="dm-1", kind=ScopeKind.PERSONAL, channel_type="D",
    )


def _memory(store: SQLiteControlPlane):
    return store.memory_items.remember(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
        scope_id=SCOPE, kind=MemoryItemKind.FACT,
        content="MMAG 项目选择 LangGraph 是为了使用持久状态与人工审批恢复。",
    )


def test_persona_publishes_an_immutable_memory_snapshot(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "persona.db"))
    memory = _memory(store)
    draft = store.personas.create_revision(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
        owner_username="alice", scope_id=SCOPE, display_name="Alice的数字人",
        allowed_topics=("MMAG",), denied_topics=("薪酬",),
        source_memory_ids=(memory.ref,),
    )
    active = store.personas.activate(draft.ref, owner_id="user-1")
    store.memory_items.revoke(memory.ref, owner_id="user-1")
    store.personas.archive_by_memory(memory.id, owner_id="user-1")

    assert active.status is DigitalPersonaStatus.ACTIVE
    assert active.published_snapshots[0]["content"] == memory.content
    assert store.personas.get(active.ref).published_snapshots == active.published_snapshots
    assert store.personas.get(active.ref).status is DigitalPersonaStatus.ARCHIVED
    assert store.personas.find_active(
        "alice", installation_id="install-1", tenant_id="tenant-1"
    ) == ()
    assert store.personas.find_active(
        "alice", installation_id="install-1", tenant_id="tenant-2"
    ) == ()
    store.close()


def test_persona_owner_can_create_select_publish_and_route_questions(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "persona-ui.db"))
    memory = _memory(store)
    tokens = ActionTokenService("s" * 32, store, ttl_seconds=60)
    ui = PersonaWorkspaceUI(
        personas=store.personas, memories=store.memory_items,
        action_tokens=tokens, audit_store=None,
    )
    scope = _scope()
    post = {
        "id": "post-1", "channel_id": "dm-1", "user_id": "user-1",
        "username": "alice", "_scope_id": SCOPE,
    }

    handled, created = ui.consume_owner(
        post,
        "创建我的数字人，允许回答 MMAG，不要回答薪酬",
        scope,
    )
    add = created.actions[0]
    ui.handle_action(
        tokens.consume(add.token, actor_id="user-1"),
        actor_id="user-1", post=post, scope=scope,
    )
    _, managed = ui.consume_owner(post, "管理我的数字人", scope)
    publish = next(action for action in managed.actions if action.action == "persona_publish")
    ui.handle_action(
        tokens.consume(publish.token, actor_id="user-1"),
        actor_id="user-1", post=post, scope=scope,
    )

    invocation, rejected = ui.resolve_question(
        "@hrzx_bot 问一下 alice 的数字人：MMAG 为什么选择 LangGraph？",
        installation_id="install-1", tenant_id="tenant-1",
    )
    denied, denial = ui.resolve_question(
        "问一下 Alice 的数字人：薪酬是多少？",
        installation_id="install-1", tenant_id="tenant-1",
    )

    assert handled and created is not None and add.action == "persona_add"
    assert memory.content in store.personas.get(publish.target).published_snapshots[0]["content"]
    assert invocation is not None and rejected is None
    assert invocation.question == "MMAG 为什么选择 LangGraph？"
    assert denied is None and denial is not None and denial.title.endswith("拒绝回答")
    store.close()


def test_persona_context_uses_only_published_snapshot(tmp_path):
    path = str(tmp_path / "persona-context.db")
    memory = Memory(path, installation_id="install-1", tenant_id="tenant-1")
    store = SQLiteControlPlane(path)
    published = _memory(store)
    private = store.memory_items.remember(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-2",
        scope_id="mattermost:install-1:tenant-1:usr:user-2",
        kind=MemoryItemKind.FACT, content="请求者自己的私人秘密",
    )
    persona = store.personas.activate(
        store.personas.create_revision(
            installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
            owner_username="alice", scope_id=SCOPE, display_name="Alice的数字人",
            source_memory_ids=(published.ref,),
        ).ref,
        owner_id="user-1",
    )

    class MM:
        @staticmethod
        def get_channel(channel_id):
            return {"id": channel_id, "type": "D", "team_id": "", "display_name": "DM"}

        @staticmethod
        def get_username(user_id):
            return user_id

        @staticmethod
        def get_user(user_id):
            return {"id": user_id, "is_bot": False}

    post = {
        "id": "question-1", "channel_id": "dm-user-2", "user_id": "user-2",
        "username": "bob", "message": "问一下 Alice 的数字人：为什么选择 LangGraph？",
        "create_at": 2, "_persona_ref": persona.ref,
        "_persona_question": "为什么选择 LangGraph？",
    }
    builder = ContextBuilder(
        MM(), memory,
        {"dm-user-2": [{**post, "message": "不应进入数字人上下文的历史内容"}]},
        BotIdentity("bot-1", "bot"), PromptAsset("test", "system", "hash", frozenset()),
        memory_items=store.memory_items, personas=store.personas,
        scope_resolver=MattermostScopeResolver(
            MM(), installation_id="install-1", tenant_id="tenant-1"
        ),
    )
    context = builder.build(post)
    rendered = "\n".join(str(item["content"]) for item in context["messages"])

    assert published.content in rendered
    assert private.content not in rendered
    assert "不应进入数字人上下文的历史内容" not in rendered
    assert "只能使用下方已发布快照" in context["system"]
    store.close()
    memory.close()


def test_high_risk_reply_requires_owner_and_can_be_edited_once(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "persona-reply.db"))
    memory = _memory(store)
    persona = store.personas.activate(
        store.personas.create_revision(
            installation_id="install-1",
            tenant_id="tenant-1",
            owner_id="user-1",
            owner_username="alice",
            scope_id=SCOPE,
            display_name="Alice的数字人",
            allowed_topics=("MMAG",),
            approval_topics=("承诺",),
            source_memory_ids=(memory.ref,),
        ).ref,
        owner_id="user-1",
    )
    ui = PersonaWorkspaceUI(
        personas=store.personas,
        memories=store.memory_items,
        action_tokens=None,
        reply_requests=store.persona_replies,
    )
    invocation, rejected = ui.resolve_question(
        "问一下 alice 的数字人：MMAG 可以承诺什么时候交付？",
        installation_id="install-1",
        tenant_id="tenant-1",
    )
    assert invocation is not None and rejected is None
    assert invocation.approval_required

    request = ui.create_reply_request(
        invocation,
        {
            "id": "question-1",
            "channel_id": "channel-1",
            "user_id": "user-2",
            "_scope_id": "mattermost:install-1:tenant-1:chn:channel-1",
        },
        requester_username="bob",
        draft_text="原始回答草稿",
        status_post_id="status-1",
    )
    with pytest.raises(PermissionError):
        ui.decide_reply(request.id, actor_id="user-2", approved=True)

    approved = ui.decide_reply(
        request.id,
        actor_id="user-1",
        approved=True,
        draft_text="本人修改后的回答",
    )
    assert approved.state is PersonaReplyState.APPROVED
    assert approved.draft_text == "本人修改后的回答"
    assert approved.persona_ref == persona.ref
    with pytest.raises(ValueError):
        ui.decide_reply(request.id, actor_id="user-1", approved=False)
    store.close()
