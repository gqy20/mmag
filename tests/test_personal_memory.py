"""Personal Memory lifecycle, isolation, UI, and context contracts."""

import pytest

from mmag.agent_packages.models import PromptAsset
from mmag.application import BotIdentity, ContextBuilder
from mmag.application.personal_ui import PersonalWorkspaceUI
from mmag.control_plane import (
    MattermostScopeResolver,
    MemoryItemKind,
    MemoryItemStatus,
    Scope,
    ScopeKind,
    SQLiteControlPlane,
)
from mmag.memory import Memory

SCOPE = "mattermost:install-1:tenant-1:usr:user-1"


def _scope(owner_id: str = "user-1") -> Scope:
    return Scope(
        f"mattermost:install-1:tenant-1:usr:{owner_id}",
        installation_id="install-1",
        tenant_id="tenant-1",
        owner_id=owner_id,
        conversation_id=f"dm-{owner_id}",
        kind=ScopeKind.PERSONAL,
        channel_type="D",
    )


def test_memory_items_are_deduplicated_isolated_and_revoked_with_source(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "memory.db"))
    first = store.memory_items.remember(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
        scope_id=SCOPE, kind=MemoryItemKind.PREFERENCE, content="我偏好简洁汇报",
        source_refs=(("mattermost_post", "post-1"),),
    )
    duplicate = store.memory_items.remember(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
        scope_id=SCOPE, kind=MemoryItemKind.PREFERENCE, content="我偏好简洁汇报",
    )

    assert duplicate.id == first.id
    assert store.memory_items.search(
        "简洁汇报", installation_id="install-1", tenant_id="tenant-1", owner_id="user-1"
    ) == (first,)
    assert store.memory_items.list_active(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-2"
    ) == ()
    assert store.memory_items.revoke_source(
        "mattermost_post", "post-1", installation_id="install-1", tenant_id="tenant-1"
    ) == 1
    assert store.memory_items.get(first.ref, owner_id="user-1").status is MemoryItemStatus.REVOKED
    store.close()


@pytest.mark.asyncio
async def test_personal_memory_is_managed_with_natural_messages(tmp_path):
    store = SQLiteControlPlane(str(tmp_path / "memory-ui.db"))
    ui = PersonalWorkspaceUI(
        personal_skills=store.personal_skills, work_cases=store.work_cases,
        interactions=store.interactions, action_tokens=None, memories=store.memory_items,
    )
    scope = _scope()
    post = {
        "id": "post-1", "channel_id": scope.conversation_id, "user_id": scope.owner_id,
        "_scope_id": scope.id,
    }

    handled, remembered, _ = await ui.consume_message(
        post, "请记住我偏好简洁的项目汇报", scope
    )
    listed, view, _ = await ui.consume_message(post, "你记得我什么", scope)
    item = store.memory_items.list_active(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1"
    )[0]
    prompted, confirmation, _ = await ui.consume_message(
        post, "忘掉关于简洁汇报的记忆", scope
    )
    forgotten = ui.handle_action(
        type("Claims", (), {
            "requested_by": "user-1", "action": "memory_forget",
            "target": confirmation.actions[0].target,
        })(),
        actor_id="user-1", post=post, scope=scope,
    )

    assert handled and remembered is not None and remembered.title == "已记住"
    assert listed and view is not None and view.title == "我的记忆"
    assert view.actions[0].label == "忘记"
    assert prompted and confirmation is not None
    assert confirmation.title == "请选择要忘记的记忆"
    assert item.ref == confirmation.actions[0].target
    assert forgotten == "这条记忆已忘记。"
    assert store.memory_items.list_active(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1"
    ) == ()
    store.close()


def test_personal_memory_is_injected_only_into_owner_dm(tmp_path):
    path = str(tmp_path / "context.db")
    memory = Memory(path, installation_id="install-1", tenant_id="tenant-1")
    store = SQLiteControlPlane(path)
    scope = _scope()
    store.memory_items.remember(
        installation_id="install-1", tenant_id="tenant-1", owner_id="user-1",
        scope_id=scope.id, kind=MemoryItemKind.PREFERENCE, content="我偏好简洁汇报",
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
        "id": "post-2", "channel_id": "dm-user-1", "user_id": "user-1",
        "username": "user-1", "message": "请按简洁风格准备汇报", "create_at": 1,
        "_scope_id": scope.id,
    }
    builder = ContextBuilder(
        MM(), memory, {"dm-user-1": [post]}, BotIdentity("bot-1", "bot"),
        PromptAsset("test", "system", "hash", frozenset()),
        memory_items=store.memory_items,
        scope_resolver=MattermostScopeResolver(
            MM(), installation_id="install-1", tenant_id="tenant-1"
        ),
    )
    context = builder.build(post)

    assert "用户明确保存的个人记忆" in context["messages"][-1]["content"]
    assert "我偏好简洁汇报" in context["messages"][-1]["content"]
    store.close()
    memory.close()
