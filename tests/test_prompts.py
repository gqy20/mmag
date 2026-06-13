"""
prompts 单元测试

覆盖:
  - PromptManager.get() 字符串模板 + .format() 变量替换
  - 身份卡占位符全部传入时能正确渲染 (bot_username / bot_user_id)
  - 占位符缺失时静默返回原模板 (.format() KeyError 走 fallback 分支)
  - prompts.yml 节点缺失或非字符串时的兜底
"""

from __future__ import annotations

import textwrap

import pytest

from mmag.prompts import PromptManager, _format_dict


@pytest.fixture
def tmp_prompts(tmp_path):
    """写入一个最小 prompts.yml 到 tmp_path 并实例化 PromptManager"""
    p = tmp_path / "prompts.yml"
    p.write_text(
        textwrap.dedent(
            """\
            system_prompt: |
              ## 身份卡（你自己）
              - 用户名: @{bot_username}
              - 用户 ID: {bot_user_id}

              ## 当前对话者
              - 用户名: @{current_user_username}
              - 用户 ID: {current_user_id}
              - 已知画像: {current_user_profile}

              ## 近期发言者
              {recent_speakers}

              你是 {bot_username}。
            """
        ),
        encoding="utf-8",
    )
    return PromptManager(p)


class TestPromptManagerGet:
    def test_renders_all_identity_placeholders(self, tmp_prompts):
        out = tmp_prompts.get(
            "system_prompt",
            bot_username="agent2",
            bot_user_id="u7qkmtkja78rdrhkn569wc4iar",
        )
        assert "@agent2" in out, "bot_username 应被替换为 @agent2"
        assert "u7qkmtkja78rdrhkn569wc4iar" in out, "bot_user_id 应被注入"
        # 占位符没残留
        assert "{bot_username}" not in out
        assert "{bot_user_id}" not in out

    def test_missing_placeholder_returns_template_silently(self, tmp_prompts):
        """模拟 agent 漏传 bot_user_id，prompts.get 不应抛 KeyError"""
        out = tmp_prompts.get(
            "system_prompt",
            bot_username="agent2",
            # bot_user_id 故意漏传
        )
        # bot_username 替换成功
        assert "@agent2" in out
        # 缺失的占位符保留原样，不崩
        assert "{bot_user_id}" in out

    def test_renders_current_user_and_speakers(self, tmp_prompts):
        """新功能：当前对话者 + 近期发言者应被注入到 system_prompt"""
        out = tmp_prompts.get(
            "system_prompt",
            bot_username="agent2",
            bot_user_id="u7qkmtkja78rdrhkn569wc4iar",
            current_user_id="u_gqy_12345678",
            current_user_username="gqy",
            current_user_profile="风格:技术型，关注:Python/Docker",
            recent_speakers="- @gqy (u_gqy_12…)\n- @whz (u_whz_34…)",
        )
        assert "@gqy" in out
        assert "u_gqy_12345678" in out
        assert "风格:技术型" in out
        # 近期发言者列表整段注入
        assert "@whz" in out
        assert "u_whz_34" in out

    def test_missing_current_user_keeps_placeholder(self, tmp_prompts):
        """没传 current_user_xxx 时应该保留 {key} 占位符（不崩）"""
        out = tmp_prompts.get(
            "system_prompt",
            bot_username="agent2",
            bot_user_id="u7qk",
            # current_user_* 全部漏传
            recent_speakers="（无）",
        )
        # 缺失的占位符保留原样
        assert "{current_user_id}" in out
        assert "{current_user_username}" in out
        assert "{current_user_profile}" in out

    def test_missing_node_returns_empty_string(self, tmp_prompts):
        assert tmp_prompts.get("nonexistent_node") == ""

    def test_non_string_node_returns_empty(self, tmp_path):
        p = tmp_path / "prompts.yml"
        p.write_text("system_prompt:\n  - a list, not a string\n", encoding="utf-8")
        pm = PromptManager(p)
        assert pm.get("system_prompt") == ""


class TestFormatDict:
    def test_recursive_replacement(self):
        data = {
            "a": "{x}",
            "b": [{"c": "{x}"}],
            "d": 123,  # 非字符串原样返回
        }
        out = _format_dict(data, {"x": "hi"})
        assert out == {"a": "hi", "b": [{"c": "hi"}], "d": 123}

    def test_missing_key_in_string_falls_back(self):
        # _format_dict 对缺失 key 的字符串静默返回原值,不抛 KeyError
        assert _format_dict("{missing}", {}) == "{missing}"
