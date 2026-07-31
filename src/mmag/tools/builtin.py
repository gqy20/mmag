"""
内置工具工厂 — 根据 MMClient 和 Memory 创建内置工具集

当前包含:
  - get_posts: 获取频道历史
  - search_knowledge: 搜索知识库
  - get_channel_info: 查询频道详情
  - save_knowledge: 写入知识库
  - get_user_profile: 查询用户画像
  - analyze_link: 分析消息中的链接 (GitHub / 通用网页)
"""

from __future__ import annotations

from ..capabilities.bindings import bind_legacy_capability
from ..capabilities.catalog import (
    create_get_channel_info_capability,
    create_get_posts_capability,
    create_get_user_profile_capability,
    create_search_knowledge_capability,
    create_search_messages_capability,
)
from ..capabilities.link import create_analyze_link_capability
from .registry import Tool

# ============================================================
# 工具参数上限（schema description / handler / 格式化 共享同一份数字）
# 改这里,所有相关地方同步更新
# ============================================================

# ============================================================
# 公共入口
# ============================================================


def build_builtin_tools(mm_client, memory) -> list[Tool]:
    """创建基于 Mattermost Client 和 Memory 的内置工具集"""

    tools = [
        _make_get_posts_tool(mm_client, memory),
        _make_search_messages_tool(memory),
        _make_search_knowledge_tool(memory),
        _make_get_channel_info_tool(mm_client),
        _make_save_knowledge_tool(memory),
        _make_get_user_profile_tool(mm_client, memory),
        _make_analyze_link_tool(memory),
    ]
    return tools


# ============================================================
# 各工具的工厂（每个 Tool 的字段较多，独立函数更易读）
# ============================================================


def _make_get_posts_tool(mm_client, memory) -> Tool:
    return bind_legacy_capability(create_get_posts_capability(mm_client, memory))


def _make_search_knowledge_tool(memory) -> Tool:
    return bind_legacy_capability(create_search_knowledge_capability(memory))


def _make_search_messages_tool(memory) -> Tool:
    return bind_legacy_capability(create_search_messages_capability(memory))


def _make_get_channel_info_tool(mm_client) -> Tool:
    return bind_legacy_capability(
        create_get_channel_info_capability(mm_client),
    )


def _make_save_knowledge_tool(memory) -> Tool:
    return Tool(
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
        handler=lambda channel_id, key, value: _save_knowledge(memory, channel_id, key, value),
    )


def _make_get_user_profile_tool(mm_client, memory) -> Tool:
    return bind_legacy_capability(create_get_user_profile_capability(mm_client, memory))


def _make_analyze_link_tool(memory) -> Tool:
    return bind_legacy_capability(create_analyze_link_capability(memory))


# ============================================================
# 工具结果格式化辅助函数
# ============================================================


def _save_knowledge(memory, channel_id: str, key: str, value: str) -> dict:
    """保存知识并返回确认"""
    memory.add_knowledge(channel_id, key, value)
    return {"status": "ok", "key": key, "message": f"已记住: {key}"}
