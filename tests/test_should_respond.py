"""
Agent._is_explicit_invocation / _is_silent 单元测试

覆盖:
  - @ 提及 → 必回
  - DM 私聊 → 必回
  - thread 回复我 → 必回
  - 普通旁听 → 不算显式召唤
  - LLM 输出 <SILENT> 解析为沉默
  - LLM 输出正常文本 → 不沉默
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.application import BotIdentity, MessageHandler  # noqa: E402
from mmag.application.agent_requests import AgentRequestHandler  # noqa: E402


def _make_handler(bot_user_id: str, bot_username: str = "agent2") -> MessageHandler:
    handler = MessageHandler.__new__(MessageHandler)
    handler.identity = BotIdentity(bot_user_id, bot_username)
    handler.memory = MagicMock()
    return handler


class FakeChannel:
    def __init__(self, type_: str = "O"):
        self._type = type_

    def get(self, key, default=None):
        if key == "type":
            return self._type
        return default


class FakeMM:
    def __init__(self, channel_type: str = "O"):
        self._channel_type = channel_type

    def get_channel(self, channel_id: str):
        return FakeChannel(self._channel_type)


# ---- _is_explicit_invocation ----


class TestIsExplicitInvocation:
    def test_at_mention_returns_true(self):
        a = _make_handler("u_bot_self")
        a.mm = FakeMM()
        post = {"channel_id": "ch1", "message": "@agent2 帮我看看", "root_id": ""}
        assert a.is_explicit_invocation(post) is True

    def test_at_mention_case_insensitive(self):
        a = _make_handler("u_bot_self")
        a.mm = FakeMM()
        post = {"channel_id": "ch1", "message": "@AGENT2 help", "root_id": ""}
        assert a.is_explicit_invocation(post) is True

    def test_dm_returns_true(self):
        a = _make_handler("u_bot_self")
        a.mm = FakeMM(channel_type="D")
        post = {"channel_id": "ch1", "message": "你好", "root_id": ""}
        assert a.is_explicit_invocation(post) is True

    def test_thread_reply_to_me_returns_true(self):
        a = _make_handler("u_bot_self")
        a.mm = FakeMM()
        a.memory.get_post_user.return_value = "u_bot_self"  # root 是我发的
        post = {"channel_id": "ch1", "message": "补充一下", "root_id": "root_xyz"}
        assert a.is_explicit_invocation(post) is True

    def test_thread_reply_to_others_returns_false(self):
        """thread 回复别人的消息 — 不算显式召唤我"""
        a = _make_handler("u_bot_self")
        a.mm = FakeMM()
        a.memory.get_post_user.return_value = "u_gqy_other"
        post = {"channel_id": "ch1", "message": "补充一下", "root_id": "root_xyz"}
        assert a.is_explicit_invocation(post) is False

    def test_plain_listen_returns_false(self):
        """群里别人聊天,没 @ 我,不是 thread — 不算显式召唤"""
        a = _make_handler("u_bot_self")
        a.mm = FakeMM()
        post = {"channel_id": "ch1", "message": "今天天气不错", "root_id": ""}
        assert a.is_explicit_invocation(post) is False

    def test_other_bot_at_returns_false(self):
        """@ 其他 bot 不是我 — 不算显式召唤"""
        a = _make_handler("u_bot_self", bot_username="agent2")
        a.mm = FakeMM()
        post = {"channel_id": "ch1", "message": "@hz_bot 你来答", "root_id": ""}
        assert a.is_explicit_invocation(post) is False


# ---- _is_silent ----


class TestIsSilent:
    def test_empty_text_is_silent(self):
        assert AgentRequestHandler.is_silent("") is True
        assert AgentRequestHandler.is_silent(None) is True

    def test_silent_marker_first_line(self):
        assert AgentRequestHandler.is_silent("<SILENT>") is True
        assert AgentRequestHandler.is_silent("<SILENT>\n") is True
        assert AgentRequestHandler.is_silent("<SILENT>") is True
        # 后面可以有空白
        assert AgentRequestHandler.is_silent("<SILENT>\n\n") is True

    def test_normal_text_not_silent(self):
        assert AgentRequestHandler.is_silent("好的,我看看") is False
        assert AgentRequestHandler.is_silent("收到 👍") is False

    def test_silent_marker_in_middle_not_silent(self):
        """<SILENT> 必须出现在第一行才视为沉默"""
        text = "我先看看情况\n<SILENT>"
        assert AgentRequestHandler.is_silent(text) is False
