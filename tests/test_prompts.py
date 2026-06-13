"""
prompts 单元测试

覆盖:
  - PromptManager.get() 字符串模板 + .format() 变量替换
  - 身份卡占位符全部传入时能正确渲染 (bot_username / bot_user_id / bot_name / bot_display_name)
  - 占位符缺失时静默返回原模板 (.format() KeyError 走 fallback 分支)
  - PromptManager.get_section() 递归 .format() 替换
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
              ## 身份卡
              - 用户名: @{bot_username}
              - 用户 ID: {bot_user_id}
              - 称呼: {bot_name}
              - 显示名: {bot_display_name}

              你是 {bot_name}。

            triggers:
              high_triggers:
                - "{bot_name}"
                - "{bot_display_name}"
              question_suffixes:
                - "?"
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
            bot_name="小智",
            bot_display_name="小智",
        )
        assert "@agent2" in out, "bot_username 应被替换为 @agent2"
        assert "u7qkmtkja78rdrhkn569wc4iar" in out, "bot_user_id 应被注入"
        assert "小智" in out, "bot_name 应被替换"
        # 占位符没残留
        assert "{bot_username}" not in out
        assert "{bot_user_id}" not in out
        assert "{bot_name}" not in out
        assert "{bot_display_name}" not in out

    def test_missing_placeholder_returns_template_silently(self, tmp_prompts):
        """模拟 agent 漏传 bot_user_id，prompts.get 不应抛 KeyError"""
        out = tmp_prompts.get(
            "system_prompt",
            bot_username="agent2",
            # bot_user_id 故意漏传
            bot_name="小智",
            bot_display_name="小智",
        )
        # bot_username 替换成功
        assert "@agent2" in out
        # 缺失的占位符保留原样，不崩
        assert "{bot_user_id}" in out

    def test_missing_node_returns_empty_string(self, tmp_prompts):
        assert tmp_prompts.get("nonexistent_node") == ""

    def test_non_string_node_returns_empty(self, tmp_path):
        p = tmp_path / "prompts.yml"
        p.write_text("system_prompt:\n  - a list, not a string\n", encoding="utf-8")
        pm = PromptManager(p)
        assert pm.get("system_prompt") == ""


class TestPromptManagerSection:
    def test_section_renders_nested_placeholders(self, tmp_prompts):
        sec = tmp_prompts.get_section(
            "triggers",
            bot_name="小智",
            bot_display_name="智哥",
        )
        # triggers 里的 {bot_name} / {bot_display_name} 都被替换
        assert "小智" in sec["high_triggers"]
        assert "智哥" in sec["high_triggers"]
        # 没有占位符残留
        for trig in sec["high_triggers"]:
            assert "{" not in trig
        # question_suffixes 不含占位符，原样保留
        assert sec["question_suffixes"] == ["?"]

    def test_section_without_kwargs_returns_raw_copy(self, tmp_prompts):
        sec = tmp_prompts.get_section("triggers")
        # 原始占位符未替换
        assert any("{bot_name}" in t for t in sec["high_triggers"])

    def test_section_missing_node_returns_empty_dict(self, tmp_prompts):
        assert tmp_prompts.get_section("nonexistent") == {}


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
