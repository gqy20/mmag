"""Memory 业务写入的事务边界测试。"""

from mmag.memory import Memory


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
