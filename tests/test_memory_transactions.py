"""Memory 业务写入的事务边界测试。"""

from mmag.memory import Memory


def test_has_message_tracks_persisted_post_ids():
    memory = Memory(":memory:")
    post = {
        "id": "post-1",
        "channel_id": "channel-1",
        "user_id": "user-1",
        "username": "alice",
        "message": "hello",
        "create_at": 1_753_929_600_000,
    }

    assert memory.has_message("post-1") is False
    assert memory.log_message(post) is True
    assert memory.has_message("post-1") is True


def test_log_message_rolls_back_main_row_when_fts_write_fails():
    memory = Memory(":memory:")
    memory._conn.execute("DROP TABLE message_log_fts")
    memory._conn.commit()

    inserted = memory.log_message(
        {
            "id": "post-1",
            "channel_id": "channel-1",
            "user_id": "user-1",
            "username": "alice",
            "message": "需要原子写入",
            "create_at": 1_753_929_600_000,
        }
    )
    row = memory._conn.execute("SELECT id FROM message_log WHERE id='post-1'").fetchone()

    assert inserted is False
    assert row is None
    assert memory._conn.in_transaction is False
    memory.close()


def test_edit_and_delete_message_keep_storage_and_fts_consistent():
    memory = Memory(":memory:", installation_id="install-a", tenant_id="tenant-a")
    post = {
        "id": "post-1",
        "channel_id": "channel-1",
        "user_id": "user-1",
        "username": "alice",
        "message": "旧内容",
        "create_at": 1_753_929_600_000,
    }
    assert memory.log_message(post) is True

    assert memory.update_message({**post, "message": "新的项目方案"}) is True
    assert [row["id"] for row in memory.search_messages("项目方案")] == ["post-1"]
    assert memory.search_messages("旧内容") == []

    assert memory.delete_message("post-1") is True
    assert memory.has_message("post-1") is False
    assert memory.search_messages("项目方案") == []
    assert memory.delete_message("post-1") is False
    memory.close()


def test_edit_and_delete_cannot_cross_memory_namespace(tmp_path):
    database = tmp_path / "memory.db"
    first = Memory(str(database), installation_id="install-a", tenant_id="tenant-a")
    second = Memory(str(database), installation_id="install-a", tenant_id="tenant-b")
    post = {
        "id": "post-1",
        "channel_id": "channel-1",
        "user_id": "user-1",
        "username": "alice",
        "message": "原始内容",
        "create_at": 1_753_929_600_000,
    }
    assert first.log_message(post) is True

    assert second.update_message({**post, "message": "越权修改"}) is False
    assert second.delete_message("post-1") is False
    assert first.get_recent_messages("channel-1")[0]["message"] == "原始内容"
    first.close()
    second.close()


def test_user_profiles_are_isolated_by_installation_and_tenant(tmp_path):
    database = tmp_path / "memory.db"
    first = Memory(str(database), installation_id="install-a", tenant_id="tenant-a")
    second = Memory(str(database), installation_id="install-a", tenant_id="tenant-b")
    post = {"message": "请整理我的技术方案", "create_at": 1_753_929_600_000}

    first.update_profile_from_message("same-user", "alice", post)

    assert first.get_user_profile("same-user")["username"] == "alice"
    assert second.get_user_profile("same-user") == {}
    first.close()
    second.close()
