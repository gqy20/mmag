"""Filter text-form reasoning and tool calls from compatible model providers."""

import re

_RE_TOOL_CALL = re.compile(
    r"(?:<tool_call>\s*.*?\s*</tool_call>|<invoke\s+.*?\s*>.*?</invoke>)",
    re.DOTALL,
)
_RE_THINKING = re.compile(r"<think(?:\s[^>]*)?>.*?</think\s*>", re.DOTALL)


def strip_tool_call_xml(text: str) -> str:
    if "<tool_call>" not in text and "<invoke" not in text:
        return text
    return _RE_TOOL_CALL.sub("", text)


def strip_thinking_tags(text: str) -> str:
    if "<think" not in text:
        return text
    return _RE_THINKING.sub("", text)


def strip_model_artifacts(text: str) -> str:
    return strip_thinking_tags(strip_tool_call_xml(text))
