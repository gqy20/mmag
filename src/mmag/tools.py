"""
工具注册系统 — 定义 Agent 可调用的工具

工具是 LLM 可以自主调用的函数，每个工具包含:
- name: 工具名（LLM 通过名称调用）
- description: 自然语言描述（LLM 靠此决定何时调用）
- input_schema: JSON Schema（校验输入参数）
- handler: 实际执行函数
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("agent")


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
        """注册一个工具"""
        if tool.name in self._tools:
            log.warning(f"工具 '{tool.name}' 已存在，将被覆盖")
        self._tools[tool.name] = tool
        log.debug(f"工具已注册: {tool.name}")

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
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        try:
            result = tool.handler(**input_data)
            # 支持 async handler
            if hasattr(result, "__aiter__") or hasattr(result, "_coro"):
                result = await result

            # 统一序列化为 JSON 字符串
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False, default=str)
            elif isinstance(result, str):
                return result
            else:
                return json.dumps({"result": result}, ensure_ascii=False, default=str)

        except Exception as e:
            log.error(f"工具 '{name}' 执行失败: {e}")
            return json.dumps(
                {"error": f"工具执行错误: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )


# ============================================================
# 内置工厂函数：根据 MMClient 和 Memory 创建内置工具集
# ============================================================


def build_builtin_tools(mm_client, memory) -> list[Tool]:
    """创建基于 Mattermost Client 和 Memory 的内置工具集"""

    tools = [
        Tool(
            name="get_posts",
            description=(
                "获取频道最近的消息历史。"
                "用于回顾讨论内容、总结对话、查找特定信息。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "频道 ID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "获取消息数量 (默认 30, 最大 100)",
                        "default": 30,
                    },
                },
                "required": ["channel_id"],
            },
            handler=lambda channel_id, limit=30: _format_posts(
                mm_client.get_posts(channel_id, min(limit, 100))
            ),
        ),
        Tool(
            name="search_knowledge",
            description=(
                "搜索团队知识库中的信息。"
                "用于查找之前记录的决策、流程、约定等知识。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "频道 ID（在哪个频道的知识库中搜索）",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回结果数量上限 (默认 5)",
                        "default": 5,
                    },
                },
                "required": ["channel_id", "query"],
            },
            handler=lambda channel_id, query, limit=5: _format_knowledge(
                memory.get_relevant_knowledge(channel_id, query, min(limit, 10))
            ),
        ),
        Tool(
            name="get_channel_info",
            description=(
                "获取频道的详细信息，包括名称、类型、成员数等。"
                "用于了解当前所在频道的基本信息。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "频道 ID",
                    },
                },
                "required": ["channel_id"],
            },
            handler=lambda channel_id: _format_channel(
                mm_client.get_channel(channel_id)
            ),
        ),
        Tool(
            name="save_knowledge",
            description=(
                "向团队知识库中存储一条知识。"
                "用于记住从对话中学到的重要事实、决策或结论。"
                "不要存储琐碎信息，只存有长期价值的内容。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "频道 ID（知识关联到哪个频道）",
                    },
                    "key": {
                        "type": "string",
                        "description": "知识的关键词/标题（如 '部署流程'）",
                    },
                    "value": {
                        "type": "string",
                        "description": "知识的详细内容",
                    },
                },
                "required": ["channel_id", "key", "value"],
            },
            handler=lambda channel_id, key, value: _save_knowledge(
                memory, channel_id, key, value
            ),
        ),
        Tool(
            name="get_user_profile",
            description=(
                "查看用户的画像信息，包括活跃度、专业领域、偏好等。"
                "用于了解团队成员的背景和特点。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "用户 ID",
                    },
                },
                "required": ["user_id"],
            },
            handler=lambda user_id: _format_profile(
                memory.get_user_profile(user_id),
                mm_client.get_username(user_id),
            ),
        ),
    ]

    return tools


# ============================================================
# 工具结果格式化辅助函数
# ============================================================


def _format_posts(posts: list[dict]) -> dict:
    """格式化消息列表为结构化输出"""
    if not posts:
        return {"count": 0, "messages": []}

    formatted = []
    for p in posts[-50:]:  # 最多返回 50 条
        formatted.append({
            "user": p.get("username", "?"),
            "message": (p.get("message") or "")[:500],
            "time": p.get("create_at", ""),
        })

    return {"count": len(formatted), "messages": formatted}


def _format_knowledge(results: list[dict]) -> dict:
    """格式化知识检索结果"""
    if not results:
        return {"count": 0, "items": [], "note": "未找到相关知识"}

    items = []
    for r in results:
        items.append({
            "key": r["key"],
            "value": r["value"],
            "confidence": r.get("_score", r.get("confidence", 0)),
        })

    return {"count": len(items), "items": items}


def _format_channel(ch: dict) -> dict:
    """格式化频道信息"""
    return {
        "id": ch.get("id", ""),
        "name": ch.get("name", ""),
        "display_name": ch.get("display_name", ""),
        "type": ch.get("type", ""),
        "type_label": {"O": "公开", "P": "私有", "D": "私聊"}.get(ch.get("type", ""), ch.get("type", "")),
    }


def _save_knowledge(memory, channel_id: str, key: str, value: str) -> dict:
    """保存知识并返回确认"""
    memory.add_knowledge(channel_id, key, value)
    return {"status": "ok", "key": key, "message": f"已记住: {key}"}


def _format_profile(profile: dict, username: str) -> dict:
    """格式化用户画像"""
    if not profile:
        return {"username": username, "note": "暂无画像信息"}

    return {
        "username": username,
        "message_count": profile.get("message_count", 0),
        "expertise": profile.get("expertise", "未知"),
        "style": profile.get("style", "未知"),
        "notes": profile.get("notes", ""),
    }
