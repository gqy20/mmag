"""
工具注册系统 — Agent 可调用的工具集合

模块结构:
  - registry: Tool dataclass + ToolRegistry
  - builtin: 内置工具工厂（get_posts, search_knowledge, get_channel_info,
             save_knowledge, get_user_profile, analyze_link）
"""

from .builtin import build_builtin_tools
from .registry import Tool, ToolRegistry

__all__ = ["Tool", "ToolRegistry", "build_builtin_tools"]
