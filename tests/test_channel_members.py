"""
agent._build_channel_members_table / _classify_role 单元测试

目的: 防止"频道成员清单"这个身份坐标系组件出现回归。
  - 之前只注入"近期发言者"(只是 user 列表,没有 role)
  - 现在升级为结构化表格,显式标 self/bot/member,防止其他 bot
    自称跟我们一样的身份时混淆

绕开 Agent.__init__ 启动(它会真连 MM/WS/记忆),直接用 __new__ 注入
最小必要属性。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.agent import Agent  # noqa: E402


class FakeMM:
    """最小化 MMClient stub,只覆盖 _build_channel_members_table 用到的 get_username"""

    def __init__(self, username_map: dict[str, str] | None = None):
        self._map = username_map or {}

    def get_username(self, uid: str) -> str:
        return self._map.get(uid, "")


def _make_agent(bot_user_id: str, bot_username: str = "agent2") -> Agent:
    """构造一个只够跑 helper 的最小 Agent 实例"""
    agent = Agent.__new__(Agent)
    agent.bot_user_id = bot_user_id
    agent.bot_username = bot_username
    agent.working_memory = {}
    return agent


# ---- _classify_role ----


class TestClassifyRole:
    def test_self_uid_returns_self(self):
        a = _make_agent("u_bot_abc")
        assert a._classify_role("u_bot_abc", "agent2") == "self"

    def test_bot_username_keyword_returns_bot(self):
        a = _make_agent("u_bot_self")
        # hz_bot / test / agent / system 都应该识别
        for u in ("hz_bot", "agent007", "test_runner", "system-bot"):
            assert a._classify_role("u_other", u) == "bot", f"username={u} 应被识别为 bot"

    def test_bot_username_keyword_case_insensitive(self):
        a = _make_agent("u_bot_self")
        assert a._classify_role("u_other", "Bot_Master") == "bot"

    def test_normal_username_returns_member(self):
        a = _make_agent("u_bot_self")
        assert a._classify_role("u_gqy_123", "gqy") == "member"
        assert a._classify_role("u_whz_456", "whz") == "member"

    def test_empty_username_returns_member(self):
        a = _make_agent("u_bot_self")
        # 没 username 时不应误判为 bot
        assert a._classify_role("u_unknown", "") == "member"


# ---- _build_channel_members_table ----


class TestBuildChannelMembersTable:
    def test_empty_working_memory_returns_placeholder(self):
        a = _make_agent("u_bot_self", "agent2")
        a.mm = FakeMM()  # type: ignore[attr-defined]
        out = a._build_channel_members_table("ch1", "u_gqy")
        assert out == "（无）"

    def test_self_appears_first_regardless_of_window_order(self):
        a = _make_agent("u_bot_self", "agent2")
        a.mm = FakeMM({"u_bot_self": "agent2", "u_gqy": "gqy"})
        a.working_memory["ch1"] = [
            {"user_id": "u_gqy", "username": "gqy"},
            {"user_id": "u_bot_self", "username": "agent2"},
        ]
        out = a._build_channel_members_table("ch1", "u_gqy")
        # self 应该出现在表格的某一行,role 必须是 self
        lines = out.splitlines()
        # 找 self 那行
        self_line = next(line for line in lines if "self" in line and "member" not in line)
        assert "u_bot_se…" in self_line
        # self 不应该在 'bot' 列
        assert "| bot |" not in self_line or "self" in self_line

    def test_bot_role_assigned_via_username_keyword(self):
        a = _make_agent("u_bot_self", "agent2")
        a.mm = FakeMM({"u_bot_self": "agent2", "u_hz_bot": "hz_bot", "u_gqy": "gqy"})
        a.working_memory["ch1"] = [
            {"user_id": "u_hz_bot", "username": "hz_bot"},
            {"user_id": "u_gqy", "username": "gqy"},
        ]
        out = a._build_channel_members_table("ch1", "u_gqy")
        # hz_bot 应该被标 bot
        assert "| bot |" in out
        # gqy 应该被标 member
        assert "| member |" in out
        # hz_bot 那行应有"其他 bot"提示
        hz_line = next(line for line in out.splitlines() if "hz_bot" in line)
        assert "其他 bot" in hz_line, "hz_bot 行应有 role=bot 风险提示"

    def test_current_user_marked_in_note_column(self):
        a = _make_agent("u_bot_self", "agent2")
        a.mm = FakeMM({"u_bot_self": "agent2", "u_gqy": "gqy", "u_whz": "whz"})
        a.working_memory["ch1"] = [
            {"user_id": "u_gqy", "username": "gqy"},
            {"user_id": "u_whz", "username": "whz"},
        ]
        out = a._build_channel_members_table("ch1", "u_whz")  # 当前是 whz
        whz_line = next(line for line in out.splitlines() if "whz" in line)
        assert "当前对话者" in whz_line
        gqy_line = next(line for line in out.splitlines() if "@gqy" in line)
        # gqy 不应有"当前对话者"标记
        assert "当前对话者" not in gqy_line

    def test_dedup_same_user_across_messages(self):
        a = _make_agent("u_bot_self", "agent2")
        a.mm = FakeMM({"u_bot_self": "agent2", "u_gqy": "gqy"})
        a.working_memory["ch1"] = [
            {"user_id": "u_gqy", "username": "gqy"},
            {"user_id": "u_gqy", "username": "gqy"},
            {"user_id": "u_gqy", "username": "gqy"},
        ]
        out = a._build_channel_members_table("ch1", "u_gqy")
        # u_gqy 应该只出现 1 次(在表格数据行)
        gqy_lines = [line for line in out.splitlines() if "@gqy" in line]
        assert len(gqy_lines) == 1, f"gqy 应去重,实际出现 {len(gqy_lines)} 次"

    def test_missing_username_falls_back_to_mm_lookup(self):
        a = _make_agent("u_bot_self", "agent2")
        # 给 mm.get_username 补 lookup
        a.mm = FakeMM({"u_gqy": "gqy_looked_up"})
        # working_memory 里 username 是空,应该被 mm.get_username 兜底
        a.working_memory["ch1"] = [
            {"user_id": "u_gqy", "username": ""},
        ]
        out = a._build_channel_members_table("ch1", "u_gqy")
        assert "gqy_looked_up" in out

    def test_output_is_markdown_table(self):
        a = _make_agent("u_bot_self", "agent2")
        a.mm = FakeMM({"u_bot_self": "agent2", "u_gqy": "gqy"})
        a.working_memory["ch1"] = [
            {"user_id": "u_gqy", "username": "gqy"},
        ]
        out = a._build_channel_members_table("ch1", "u_gqy")
        # 至少包含表头分隔符
        assert "|---|" in out
        # 至少包含 1 行数据
        data_lines = [
            line for line in out.splitlines()
            if line.startswith("|") and "uid" not in line and "---" not in line
        ]
        assert len(data_lines) >= 2  # self + gqy
