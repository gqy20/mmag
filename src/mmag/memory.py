"""
SQLite 持久化记忆
"""

import json
import logging
import sqlite3
import time
from typing import Optional

log = logging.getLogger("agent")


class Memory:
    """SQLite 持久化记忆"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        c = self._conn.cursor()
        # 对话片段
        c.execute("""
            CREATE TABLE IF NOT EXISTS conversation_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                started_at REAL,
                ended_at REAL,
                topic TEXT,
                summary TEXT,
                participants TEXT,
                key_points TEXT,
                status TEXT DEFAULT 'active'
            )
        """)
        # 待办事项
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT,
                description TEXT,
                mentioned_by TEXT,
                created_at REAL,
                resolved_at REAL,
                resolution TEXT
            )
        """)
        # 用户画像
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_seen REAL,
                message_count INTEGER DEFAULT 0,
                expertise TEXT,
                preferences TEXT,
                style TEXT DEFAULT 'casual',
                notes TEXT,
                last_interaction REAL
            )
        """)
        # 团队知识
        c.execute("""
            CREATE TABLE IF NOT EXISTS team_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT,
                key TEXT,
                value TEXT,
                source TEXT DEFAULT 'conversation',
                confidence REAL DEFAULT 0.5,
                updated_at REAL,
                mentioned_count INTEGER DEFAULT 0
            )
        """)
        # 消息历史缓存（短期）
        c.execute("""
            CREATE TABLE IF NOT EXISTS message_cache (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                user_id TEXT,
                username TEXT,
                message TEXT,
                create_at REAL,
                post_type TEXT DEFAULT '',
                root_id TEXT DEFAULT ''
            )
        """)
        self._conn.commit()
        log.info(f"记忆数据库就绪: {self.db_path}")

    # ---- 消息缓存 ----

    def cache_message(self, post: dict):
        msg = post.get("message", "") or ""
        if not msg or len(msg) > 50000:
            return
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO message_cache
                   (id, channel_id, user_id, username, message, create_at, post_type, root_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post["id"],
                    post.get("channel_id", ""),
                    post.get("user_id", ""),
                    "",  # username 需要另外查
                    msg[:10000],
                    (post.get("create_at", 0) or 0) / 1000.0,
                    post.get("type", ""),
                    post.get("root_id", ""),
                ),
            )
            self._conn.commit()
        except Exception as e:
            log.debug(f"缓存消息失败: {e}")

    def get_recent_messages(self, channel_id: str, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM message_cache WHERE channel_id=? ORDER BY create_at DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_channel_cache(self, channel_id: str):
        self._conn.execute("DELETE FROM message_cache WHERE channel_id=?", (channel_id,))
        self._conn.commit()

    # ---- 用户画像 ----

    def update_user_profile(self, user_id: str, username: str, updates: dict):
        existing = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        now = time.time()
        if existing:
            data = dict(existing)
            for k, v in updates.items():
                if v and v != {"no_change": True}:
                    old = data.get(k)
                    if isinstance(old, str):
                        try:
                            old_json = json.loads(old)
                        except Exception:
                            old_json = {}
                        if isinstance(v, dict):
                            old_json.update(v)
                            v = json.dumps(v, ensure_ascii=False)
                    if v:
                        self._conn.execute(
                            f"UPDATE user_profiles SET {k}=?, message_count=message_count+1, "
                            "last_interaction=? WHERE user_id=?",
                            (v, now, user_id),
                        )
                    elif k == "message_count":
                        self._conn.execute(
                            "UPDATE user_profiles SET message_count=message_count+1, "
                            "last_interaction=? WHERE user_id=?",
                            (now, user_id),
                        )
        else:
            self._conn.execute(
                """INSERT OR IGNORE INTO user_profiles
                   (user_id, username, first_seen, message_count, last_interaction)
                   VALUES (?, ?, ?, 1, ?)""",
                (user_id, username, now, now),
            )
        self._conn.commit()

    def get_user_profile(self, user_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else {}

    # ---- 团队知识 ----

    def add_knowledge(self, channel_id: str, key: str, value: str, confidence: float = 0.5):
        now = time.time()
        existing = self._conn.execute(
            "SELECT id, mentioned_count FROM team_knowledge WHERE channel_id=? AND key=?",
            (channel_id, key),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE team_knowledge SET value=?, confidence=?, updated_at=?, "
                "mentioned_count=mentioned_count+1 WHERE id=?",
                (value, confidence, now, existing["id"]),
            )
        else:
            self._conn.execute(
                """INSERT INTO team_knowledge (channel_id, key, value, confidence, updated_at, mentioned_count)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (channel_id, key, value, confidence, now),
            )
        self._conn.commit()

    def get_relevant_knowledge(self, channel_id: str, query: str, limit: int = 5) -> list[dict]:
        # 简单关键词匹配（后续可升级为向量搜索）
        keywords = set(query.lower().split())
        rows = self._conn.execute(
            "SELECT * FROM team_knowledge WHERE channel_id=? ORDER BY mentioned_count DESC LIMIT ?",
            (channel_id, limit * 3),
        ).fetchall()
        scored = []
        for r in rows:
            d = dict(r)
            text = (d["key"] + " " + d["value"]).lower()
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                d["_score"] = score
                scored.append(d)
        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored[:limit]

    # ---- 短期记忆摘要 ----

    def save_conversation_segment(self, channel_id: str, topic: str, summary: str,
                                    participants: list, key_points: list):
        now = time.time()
        self._conn.execute(
            """INSERT INTO conversation_segments
               (channel_id, started_at, topic, summary, participants, key_points)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel_id, now - 300, topic, summary,
             json.dumps(participants, ensure_ascii=False),
             json.dumps(key_points, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_recent_summary(self, channel_id: str) -> str:
        row = self._conn.execute(
            """SELECT summary FROM conversation_segments
               WHERE channel_id=? AND status='active'
               ORDER BY started_at DESC LIMIT 1""",
            (channel_id,),
        ).fetchone()
        return row["summary"] if row else ""

    # ---- 清理 ----

    def clear_all(self):
        for table in ["message_cache", "conversation_segments", "open_items"]:
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()
        log.info("短期记忆已清空")

    def close(self):
        if self._conn:
            self._conn.close()
