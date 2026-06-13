"""
llm 模块过滤函数单元测试

覆盖:
  - _strip_thinking_tags: step-3.7-flash 等模型把思考过程作为普通 text 输出
    包裹在 <think>...</think> 里, 需要在返回频道前剥掉
  - _strip_model_artifacts: 组合入口, 一次剥掉 tool_call XML + thinking 标签
"""

from __future__ import annotations

from mmag.llm import _strip_model_artifacts, _strip_thinking_tags, _strip_tool_call_xml


class TestStripThinkingTags:
    def test_simple_think_block_is_removed(self):
        text = "<think>用户让我看下报错</think>好的,我看一下。"
        out = _strip_thinking_tags(text)
        assert "<think>" not in out
        assert "用户让我看下报错" not in out
        assert "好的,我看一下。" in out

    def test_multiline_think_block_is_removed(self):
        text = (
            "<think>\n"
            "用户报了 TypeError,可能跟变量未定义有关。\n"
            "我先问下完整堆栈。\n"
            "</think>\n我先看一下堆栈信息。"
        )
        out = _strip_thinking_tags(text)
        assert "TypeError" not in out
        assert "<think>" not in out
        assert "我先看一下堆栈信息。" in out

    def test_multiple_think_blocks_all_removed(self):
        text = "<think>内部独白 1</think>中间可见内容<think>内部独白 2</think>结尾"
        out = _strip_thinking_tags(text)
        assert "内部独白" not in out
        assert "中间可见内容" in out
        assert "结尾" in out

    def test_no_think_tag_returns_input_unchanged(self):
        text = "普通回答,没有任何 think 标签"
        assert _strip_thinking_tags(text) == text

    def test_unclosed_think_tag_is_left_alone(self):
        """非贪婪匹配, 缺结束标签时不剥 (避免误伤)"""
        text = "<think>没有结束标签,可能输出到这就被截断了"
        out = _strip_thinking_tags(text)
        # 没匹配到,整段保留
        assert "<think>" in out
        assert "没有结束标签" in out

    def test_think_around_real_content(self):
        """think 块前后是真实可见文本, 应只剥中间"""
        text = "前缀<think>思考过程</think>后缀"
        out = _strip_thinking_tags(text)
        assert out == "前缀后缀"

    def test_only_think_returns_empty(self):
        """整段都是 think, 剥后空, 由调用方走兜底"""
        out = _strip_thinking_tags("<think>只有内心独白</think>")
        assert out == ""

    def test_fast_path_when_no_think_substring(self):
        """没有 '<think>' 子串时应直接返回, 不进 regex"""
        # 用一个明显不会匹配的长文本, 确认不进 regex 也正确
        text = "a" * 5000
        assert _strip_thinking_tags(text) == text


class TestStripToolCallXml:
    """对称覆盖 - 保证 _strip_tool_call_xml 行为没回归(只动 _strip_model_artifacts 的话)"""

    def test_simple_tool_call_block_removed(self):
        text = '<tool_call>{"name": "x"}</tool_call>好的'
        out = _strip_tool_call_xml(text)
        assert "<tool_call>" not in out
        assert "好的" in out

    def test_no_tag_unchanged(self):
        assert _strip_tool_call_xml("hello") == "hello"


class TestStripModelArtifacts:
    """组合入口 - 一次剥掉所有已知的训练痕迹"""

    def test_think_only(self):
        text = "<think>内心独白</think>公开答复"
        assert _strip_model_artifacts(text) == "公开答复"

    def test_tool_call_only(self):
        text = '<tool_call>{"name":"x"}</tool_call>公开答复'
        assert _strip_model_artifacts(text) == "公开答复"

    def test_both_think_and_tool_call_removed(self):
        """step-3.7-flash 偶发两个痕迹同时出现, 都应剥掉"""
        text = (
            "<think>先想一下要怎么调工具</think>"
            '<tool_call>{"name":"search","arguments":{}}</tool_call>'
            "好的,我查一下"
        )
        out = _strip_model_artifacts(text)
        assert "<think>" not in out
        assert "<tool_call>" not in out
        assert "先想一下" not in out
        assert '{"name":"search"}' not in out
        assert "好的,我查一下" in out

    def test_clean_text_unchanged(self):
        text = "正常回答,没有任何痕迹"
        assert _strip_model_artifacts(text) == text

    def test_only_artifacts_returns_empty(self):
        text = "<think>独白</think><tool_call>{}</tool_call>"
        assert _strip_model_artifacts(text) == ""

    def test_order_does_not_matter_for_disjoint_blocks(self):
        """tool_call + think 顺序不影响剥离结果"""
        a = "<tool_call>{}</tool_call><think>独白</think>好"
        b = "<think>独白</think><tool_call>{}</tool_call>好"
        assert _strip_model_artifacts(a) == _strip_model_artifacts(b) == "好"
