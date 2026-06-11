"""
SQLite 持久化记忆
"""

import json
import sqlite3
import time

from .logger import get_logger

log = get_logger(__name__)


class Memory:
    """SQLite 持久化记忆"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
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
        # 用户画像（含自动推断字段）
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_seen REAL,
                message_count INTEGER DEFAULT 0,
                expertise TEXT,
                preferences TEXT,
                style TEXT DEFAULT 'casual',
                notes TEXT,
                last_interaction REAL,
                topics TEXT,
                active_hours TEXT,
                _question_count INTEGER DEFAULT 0
            )
            """
        )
        # 团队知识
        c.execute(
            """
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
            """
        )

        # FTS5 全文搜索虚拟表（与 team_knowledge 同步维护）
        # 注意: 中文文本需预分词（插入空格），否则 FTS5 将整段中文视为一个 token
        c.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS team_knowledge_fts USING fts5(
                key,
                value,
                content='team_knowledge',
                content_rowid='id'
            )
            """
        )
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
        # URL 分析结果缓存（GitHub API / 通用网页）
        c.execute("""
            CREATE TABLE IF NOT EXISTS url_cache (
                url TEXT PRIMARY KEY,
                url_hash TEXT NOT NULL,
                kind TEXT,
                status TEXT,
                title TEXT,
                summary TEXT,
                content TEXT,
                metadata TEXT,
                fetched_at REAL,
                expires_at REAL,
                error TEXT
            )
        """)
        self._conn.commit()
        log.info(f"记忆数据库就绪: {self.db_path}")

    # ---- 消息缓存 ----

    def cache_message(self, post: dict):
        """缓存单条消息到 message_cache 表

        期望 post 包含完整 username（由调用方在写入前从 MMClient.get_username 补全），
        避免读取时再去走 REST API。
        """
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
                    post.get("username", ""),
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

    def get_channel_cache_count(self, channel_id: str) -> int:
        """获取某频道缓存的消息总数"""
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM message_cache WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def pop_old_messages(self, channel_id: str, keep: int = 800) -> list[dict]:
        """取出并删除最旧的消息（保留最新的 keep 条）

        用于 Layer 1→Layer 2 压缩：将超限的旧消息取出做 LLM 摘要，
        摘要完成后这些消息就从 message_cache 中移除，释放空间。

        Returns:
            被取出的消息列表（按时间正序，最旧的在前）
        """
        # 先查出要删除的最旧消息 ID
        total = self.get_channel_cache_count(channel_id)
        if total <= keep:
            return []

        pop_count = total - keep
        rows = self._conn.execute(
            "SELECT * FROM message_cache WHERE channel_id=? ORDER BY create_at ASC LIMIT ?",
            (channel_id, pop_count),
        ).fetchall()

        if not rows:
            return []

        ids_to_delete = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids_to_delete))
        self._conn.execute(
            f"DELETE FROM message_cache WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        self._conn.commit()

        log.debug(
            "消息压缩: channel=%s 弹出 %d 条 (保留 %d 条), 剩余 %d 条",
            channel_id[:12],
            len(rows),
            keep,
            self.get_channel_cache_count(channel_id),
        )
        return [dict(r) for r in rows]

    def peek_recent_messages(self, channel_id: str, limit: int = 100) -> list[dict]:
        """只读获取最近 N 条消息（不删除）

        用于 Layer 2 定期摘要：每累积一定数量消息后，
        取出最近一批做 LLM 摘要，原始消息保留在缓存中。
        """
        rows = self._conn.execute(
            "SELECT * FROM message_cache WHERE channel_id=? ORDER BY create_at DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
        # 返回时间正序（旧的在前）
        return [dict(r) for r in reversed(rows)]

    # ---- URL 缓存（url_analyzer 写入）----

    # URL 缓存: content 字段上限 (字节)。SQLite TEXT 实际无硬限制, 这里设软上限
    # 防止某些超大 README (数 MB) 把数据库撑爆。一般仓库 README 都在 200KB 以内。
    _URL_CACHE_CONTENT_MAX = 2_000_000  # 2 MB

    def cache_url(self, url: str, info: dict, ttl_seconds: int = 3600) -> None:
        """缓存一个 URL 的分析结果

        info 必须包含: kind, status, title, summary, metadata (dict)
        可选: error
        长度守卫: content 字段截断到 2MB (防止巨型 README/页面撑爆数据库)。
                  summary 字段保持完整 (在 url_analyzer 层不再截断)。
        错误处理: 任何异常只 log.debug，不向上抛。
        """
        if not url or not info:
            return
        try:
            import hashlib

            metadata = info.get("metadata") or {}
            content = info.get("content") or ""
            if isinstance(content, str) and len(content) > self._URL_CACHE_CONTENT_MAX:
                content = content[: self._URL_CACHE_CONTENT_MAX]
            now = time.time()
            self._conn.execute(
                """INSERT OR REPLACE INTO url_cache
                   (url, url_hash, kind, status, title, summary, content, metadata,
                    fetched_at, expires_at, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    url,
                    hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                    info.get("kind", "unknown"),
                    info.get("status", "ok"),
                    (info.get("title") or "")[:500],
                    (info.get("summary") or "")[:5000],  # 缓存层快速预览上限
                    content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False, default=str)[:50000],
                    now,
                    now + ttl_seconds,
                    info.get("error") or "",
                ),
            )
            self._conn.commit()
        except Exception as e:
            log.debug(f"缓存 URL 失败: {e}")

    def get_cached_url(self, url: str) -> dict | None:
        """获取缓存的 URL 分析结果（自动判断过期）

        返回 dict: {kind, status, title, summary, content, metadata, fetched_at, expires_at, error, cached=True}
        过期或不存在 → 返回 None。
        错误处理: 任何异常只 log.debug，不向上抛。
        """
        if not url:
            return None
        try:
            row = self._conn.execute("SELECT * FROM url_cache WHERE url=?", (url,)).fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("expires_at", 0) < time.time():
                # 已过期 — 视为未命中（不主动删除，让下次写入覆盖）
                return None
            # 解析 metadata JSON
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            d["cached"] = True
            return d
        except Exception as e:
            log.debug(f"读取 URL 缓存失败: {e}")
            return None

    # ---- 用户画像（从消息行为自动推断）----

    def update_profile_from_message(self, user_id: str, username: str, post: dict):
        """从单条消息推断并更新用户画像

        每次收到用户消息时调用，逐步积累以下维度:
          - topics: 话题标签（从消息内容提取关键词）
          - active_hours: 活跃时段分布
          - style: 沟通风格（简洁/详细/技术型）
          - message_count / last_interaction: 基础统计
        """
        import re
        from datetime import datetime

        now = time.time()
        msg = (post.get("message") or "").strip()
        create_at = post.get("create_at", 0) or 0

        # ---- 提取信号 ----
        # 1. 消息长度特征
        msg_len = len(msg)
        has_code = bool(re.search(r"```|`[^`]+`", msg))
        has_link = bool(re.search(r"https?://\S+", msg))
        is_question = msg.endswith(("?", "？", "吗", "呢")) or any(
            w in msg for w in ("什么", "怎么", "如何", "为什么", "哪", "谁")
        )

        # 2. 话题关键词（简单词频，取有意义的中文词和英文词）
        topic_words = self._extract_topic_keywords(msg)

        # 3. 活跃时段
        hour = None
        if create_at > 0:
            try:
                dt = datetime.fromtimestamp(create_at / 1000.0 if create_at > 1e12 else create_at)
                hour = dt.hour
            except (OSError, ValueError):
                pass

        # ---- 构建更新数据 ----
        existing = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()

        if existing:
            prof = dict(existing)

            import contextlib

            # 累积话题（合并去重）
            old_topics = []
            if prof.get("topics"):
                with contextlib.suppress(Exception):
                    old_topics = json.loads(prof["topics"])
            for w in topic_words:
                if w not in old_topics:
                    old_topics.append(w)
            # 只保留最近 20 个话题
            new_topics = json.dumps(old_topics[-20:], ensure_ascii=False)

            # 累积活跃时段
            old_hours: dict = {}
            if prof.get("active_hours"):
                with contextlib.suppress(Exception):
                    old_hours = json.loads(prof["active_hours"])
            if hour is not None:
                h_str = f"{hour:02d}:00"
                old_hours[h_str] = old_hours.get(h_str, 0) + 1
            new_hours = json.dumps(old_hours, ensure_ascii=False)

            # 推断沟通风格（基于历史统计）
            # 追踪问句计数
            q_count = prof.get("_question_count", 0) or 0
            if is_question:
                q_count += 1

            style = self._infer_style(prof, msg_len, has_code, has_link, is_question, q_count)

            # 更新数据库
            self._conn.execute(
                """UPDATE user_profiles SET
                   topics=?, active_hours=?,
                   style=?,
                   _question_count=?,
                   message_count=message_count+1,
                   last_interaction=?
                   WHERE user_id=?""",
                (new_topics, new_hours, style, q_count, now, user_id),
            )
        else:
            # 首次创建画像
            topics_json = json.dumps(topic_words[:10], ensure_ascii=False)
            hours_json = json.dumps(
                {f"{hour:02d}:00": 1} if hour is not None else {}, ensure_ascii=False
            )
            q_count = 1 if is_question else 0
            style = self._initial_style(msg_len, has_code, has_link, is_question, q_count)

            self._conn.execute(
                """INSERT INTO user_profiles
                   (user_id, username, first_seen, message_count,
                    topics, active_hours, style, _question_count, notes, last_interaction)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    username,
                    now,
                    topics_json,
                    hours_json,
                    style,
                    q_count,
                    "首次出现",
                    now,
                ),
            )

        self._conn.commit()

    @staticmethod
    def _extract_topic_keywords(text: str, max_words: int = 5) -> list[str]:
        """从消息中提取有意义的主题关键词

        规则:
          - 过滤停用词（的/了/是/在...）
          - 过滤单字（除非是英文缩写如 k8s）
          - 过滤纯数字
          - 过滤 @提及 和 #频道
          - 返回最长的前 N 个词（长词通常更有信息量）
        """
        import re

        stop = {
            "的",
            "了",
            "是",
            "在",
            "我",
            "有",
            "和",
            "就",
            "不",
            "人",
            "都",
            "一",
            "个",
            "上",
            "也",
            "很",
            "到",
            "说",
            "要",
            "去",
            "你",
            "会",
            "着",
            "没有",
            "看",
            "好",
            "自己",
            "这",
            "他",
            "她",
            "它",
            "们",
            "那",
            "什么",
            "怎么",
            "如何",
            "为什么",
            "可以",
            "这个",
            "那个",
            "这些",
            "那些",
            "如果",
            "因为",
            "但是",
            "然后",
            "或者",
            "已经",
            "还是",
            "需要",
            "应该",
            "可能",
            "知道",
            "觉得",
            "想",
            "做",
            "用",
            "来",
            "对",
            "吧",
            "啊",
            "呢",
            "哈",
            "嗯",
            "哦",
            "呀",
            "嘛",
            "啦",
            "诶",
            "喂",
            "嗨",
            "ok",
            "OK",
        }
        # 提取中文词（>=2字）和英文词
        words = re.findall(r"[a-zA-Z]{2,}|[一-鿿]{2,}", text)
        filtered = [w for w in words if w.lower() not in stop and not w.isdigit()]
        # 按长度降序，取前几个（长词优先）
        filtered.sort(key=len, reverse=True)
        return filtered[:max_words]

    @staticmethod
    def _infer_style(
        existing: dict,
        msg_len: int,
        has_code: bool,
        has_link: bool,
        is_question: bool,
        question_count: int = 0,
    ) -> str:
        """基于历史 + 当前消息推断沟通风格"""
        count = existing.get("message_count", 0) + 1
        old_style = existing.get("style", "")

        # 从已有风格继承基础判断
        parts = set()
        if old_style:
            for s in old_style.split("/"):
                s = s.strip()
                if s:
                    parts.add(s)

        # 消息长度判断
        if msg_len > 200:
            parts.add("详细")
        elif msg_len < 20:
            parts.add("简洁")
        elif msg_len > 80:
            parts.discard("简洁")

        # 技术特征
        if has_code:
            parts.add("技术型")
        if has_link:
            parts.add("爱分享链接")

        # 行为倾向（>=3 条消息即可开始判断）
        if count >= 3 and question_count > 0:
            q_rate = question_count / count
            if q_rate >= 0.4:
                parts.add("常提问")

        return "/".join(sorted(parts)) if parts else "casual"

    @staticmethod
    def _initial_style(
        msg_len: int, has_code: bool, has_link: bool, is_question: bool, question_count: int = 0
    ) -> str:
        """首次画像时的初始风格"""
        parts = []
        if msg_len > 100:
            parts.append("详细")
        elif msg_len < 20:
            parts.append("简洁")
        if has_code:
            parts.append("技术型")
        if has_link:
            parts.append("爱分享链接")
        if is_question or question_count > 0:
            parts.append("常提问")
        return "/".join(parts) if parts else "casual"

    def get_user_profile(self, user_id: str) -> dict:
        """原始画像：topics/active_hours 仍是 JSON 字符串，调用方需自己解析"""
        row = self._conn.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else {}

    def get_user_profile_decoded(self, user_id: str) -> dict:
        """画像（已解析 JSON 字段）

        与 get_user_profile 的区别: topics (list) 与 active_hours (dict) 已 json.loads,
        解析失败时各自 fallback 到空类型。供展示层 (tool handler / 格式化) 直接消费。
        """
        profile = self.get_user_profile(user_id)
        if not profile:
            return {}
        for key, default in (("topics", []), ("active_hours", {})):
            raw = profile.get(key)
            if not raw:
                profile[key] = default
                continue
            try:
                profile[key] = json.loads(raw)
            except Exception:
                profile[key] = default
        return profile

    # ---- 团队知识 ----

    def add_knowledge(self, channel_id: str, key: str, value: str, confidence: float = 0.5):
        now = time.time()
        existing = self._conn.execute(
            "SELECT id, mentioned_count FROM team_knowledge WHERE channel_id=? AND key=?",
            (channel_id, key),
        ).fetchone()
        if existing:
            rowid = existing["id"]
            self._conn.execute(
                "UPDATE team_knowledge SET value=?, confidence=?, updated_at=?, "
                "mentioned_count=mentioned_count+1 WHERE id=?",
                (value, confidence, now, rowid),
            )
            # 同步更新 FTS5 虚拟表（CJK 预分词）
            self._conn.execute(
                "UPDATE team_knowledge_fts SET key=?, value=? WHERE rowid=?",
                (_cjk_tokenize_for_fts(key), _cjk_tokenize_for_fts(value), rowid),
            )
        else:
            cursor = self._conn.execute(
                """INSERT INTO team_knowledge (channel_id, key, value, confidence, updated_at, mentioned_count)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (channel_id, key, value, confidence, now),
            )
            # 同步写入 FTS5 虚拟表（CJK 预分词）
            rowid = cursor.lastrowid
            self._conn.execute(
                "INSERT INTO team_knowledge_fts(rowid, key, value) VALUES (?, ?, ?)",
                (rowid, _cjk_tokenize_for_fts(key), _cjk_tokenize_for_fts(value)),
            )
        self._conn.commit()

    def get_relevant_knowledge(self, channel_id: str, query: str, limit: int = 5) -> list[dict]:
        """FTS5 全文搜索 — 利用 SQLite 内置 BM25 排序算法

        FTS5 自动处理:
          - 中英文分词与索引
          - BM25 相关性评分（TF-IDF 变体，业界成熟方案）
          - 前缀/短语/NEAR 查询支持
          - 结果按相关性自动排序

        知识库通常只有几十条，即使 MATCH 无命中也会 fallback 返回最近的知识，
        让 LLM 自行判断是否相关。
        """
        try:
            fts_query = _cjk_tokenize_for_fts(query)
            rows = self._conn.execute(
                """
                SELECT tk.*, rank
                FROM team_knowledge tk
                INNER JOIN team_knowledge_fts fts ON tk.id = fts.rowid
                WHERE tk.channel_id=?
                  AND team_knowledge_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (channel_id, fts_query, limit),
            ).fetchall()

            results = []
            for r in rows:
                d = dict(r)
                d["_score"] = d.pop("rank", 0)  # FTS5 rank 即 BM25 分
                results.append(d)

            return results
        except Exception as e:
            log.debug("FTS5 查询异常 (%s), fallback 全量返回: %s", channel_id, e)
            # FTS5 查询失败时降级为全量返回（让 LLM 自己筛选）
            rows = self._conn.execute(
                "SELECT * FROM team_knowledge WHERE channel_id=? "
                "ORDER BY mentioned_count DESC LIMIT ?",
                (channel_id, limit * 2),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- 短期记忆摘要 ----

    def save_conversation_segment(
        self, channel_id: str, topic: str, summary: str, participants: list, key_points: list
    ):
        now = time.time()
        self._conn.execute(
            """INSERT INTO conversation_segments
               (channel_id, started_at, topic, summary, participants, key_points)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                channel_id,
                now - 300,
                topic,
                summary,
                json.dumps(participants, ensure_ascii=False),
                json.dumps(key_points, ensure_ascii=False),
            ),
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
        """清空短期对话数据（保留用户画像和团队知识）

        删除: message_cache (消息缓存), conversation_segments (对话段),
              open_items (待办), url_cache (链接分析缓存)
        保留: user_profiles (用户画像), team_knowledge (团队知识库) — 这些是长期数据
        """
        for table in ["message_cache", "conversation_segments", "open_items", "url_cache"]:
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()
        log.info("短期对话数据已清空（用户画像/团队知识已保留）")

    def close(self):
        if self._conn:
            self._conn.close()


# ============================================================
# CJK 预分词（FTS5 中文支持）
# ============================================================


def _cjk_tokenize_for_fts(text: str) -> str:
    """为 FTS5 预处理中文文本：在 CJK 字符间插入空格

    FTS5 默认 tokenizer (unicode61) 按空格/标点分词，
    对无空格的中文文本会整段视为一个 token，导致部分匹配失败。

    本函数在 CJK 字符之间插入空格，使 FTS5 能逐字索引:
      "部署流程" → "部 署 流 程"
      "k8s集群部署" → "k8s 集 群 部 署"

    英文/数字保持不变（它们本身有空格分隔）。
    """
    import re

    result = []
    prev_is_cjk = False
    for ch in text:
        is_cjk = bool(re.match(r"[一-鿿㐀-䶿]", ch))
        if is_cjk and prev_is_cjk:
            # 连续 CJK 字符之间插空格
            result.append(f" {ch}")
        else:
            result.append(ch)
        prev_is_cjk = is_cjk
    return "".join(result)
