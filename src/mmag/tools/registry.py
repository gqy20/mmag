"""
Tool dataclass + ToolRegistry — 工具抽象与执行

工具是 LLM 可以自主调用的函数，每个工具包含:
- name: 工具名（LLM 通过名称调用）
- description: 自然语言描述（LLM 靠此决定何时调用）
- input_schema: JSON Schema（校验输入参数）
- handler: 实际执行函数（支持 sync / async）
"""

from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from ..logger import get_logger, trace

log = get_logger(__name__)


@dataclass
class Tool:
    """单个工具定义"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    # 是否将执行结果反馈给用户（某些内部工具不需要）
    visible: bool = True


class ToolRegistry:
    """工具注册表 — 管理所有可用工具"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具"""
        if tool.name in self._tools:
            log.warning("工具 '%s' 已存在，将被覆盖", tool.name)
        self._tools[tool.name] = tool
        log.info(
            "工具已注册: %s (%d 参数)", tool.name, len(tool.input_schema.get("properties", {}))
        )

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否存在并被删除"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def unregister_prefix(self, prefix: str) -> int:
        """注销所有 name 以 prefix 开头的工具，返回注销数量"""
        names = [n for n in self._tools if n.startswith(prefix)]
        for n in names:
            del self._tools[n]
        return len(names)

    def get_all(self) -> list[Tool]:
        """获取所有已注册的工具"""
        return list(self._tools.values())

    def get_schema_list(self) -> list[dict[str, Any]]:
        """获取 Anthropic API 格式的工具定义列表（用于 LLM 调用）"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def get_descriptions(self) -> str:
        """生成工具描述文本（用于注入 system prompt）"""
        lines = []
        for t in self._tools.values():
            params = ", ".join(t.input_schema.get("properties", {}).keys())
            desc = t.description
            lines.append(f"- **{t.name}**({params}): {desc}")
        return "\n".join(lines)

    async def execute(self, name: str, input_data: dict[str, Any]) -> str:
        """
        执行指定工具并返回结果字符串。

        Returns:
            工具执行的 JSON 字符串结果，或错误信息。
        """
        tool = self._tools.get(name)
        if not tool:
            log.warning("%s 未知工具: %s", trace.prefix(), name)
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        t0 = time.monotonic()
        log.info(
            "%s 调用工具: %s(%s)",
            trace.prefix(),
            name,
            json.dumps(input_data, ensure_ascii=False)[:200],
        )

        try:
            result = tool.handler(**input_data)
            # 支持 async handler / async generator
            if inspect.iscoroutine(result) or inspect.isasyncgen(result):
                result = await result

            # 统一序列化为 JSON 字符串
            if isinstance(result, (dict, list)):
                result_str = json.dumps(result, ensure_ascii=False, default=str)
            elif isinstance(result, str):
                result_str = result
            else:
                result_str = json.dumps({"result": result}, ensure_ascii=False, default=str)

            elapsed = time.monotonic() - t0
            log.info(
                "%s 工具完成: %s (%.3fs, 结果 %d 字符)",
                trace.prefix(),
                name,
                elapsed,
                len(result_str),
            )
            return result_str

        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error(
                "%s 工具 '%s' 执行失败 (%.3fs): %s", trace.prefix(), name, elapsed, e, exc_info=True
            )
            return json.dumps(
                {"error": f"工具执行错误: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
