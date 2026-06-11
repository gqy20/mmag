"""
统一日志系统 — 分层 Logger + 可选文件输出 + 交互追踪

设计原则:
  - 每个模块用自己的 logger 名 (mmag.config, mmag.llm, mmag.tools ...)
  - 统一初始化，不再依赖 cli.py 的 if __name__
  - 支持同时输出到控制台 + 文件
  - 每次用户交互带 msg_id，贯穿全链路
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

# 包根名
PKG_NAME = "mmag"

# 全局标志：是否已初始化
_initialized = False


def get_logger(name: str) -> logging.Logger:
    """获取分层 Logger

    用法:
        from mmag.logger import get_logger
        log = get_logger(__name__)   # → "mmag.agent" 或 "mmag.tools" 等
    """
    if name.startswith(PKG_NAME + "."):
        full_name = name
    elif name == PKG_NAME:
        full_name = PKG_NAME
    else:
        full_name = f"{PKG_NAME}.{name}"
    return logging.getLogger(full_name)


def init_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    fmt: Optional[str] = None,
    datefmt: Optional[str] = None,
) -> None:
    """统一初始化日志系统（幂等，多次调用只生效一次）

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        log_file: 日志文件路径（None 则不写文件）
        fmt: 自定义格式串
        datefmt: 时间格式
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    _fmt = fmt or "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
    _datefmt = datefmt or "%H:%M:%S"

    root_logger = logging.getLogger(PKG_NAME)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有 handler（防止重复）
    root_logger.handlers.clear()

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(_fmt, datefmt=_datefmt))
    root_logger.addHandler(console)

    # 文件 handler（可选）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        # 文件用更详细的格式（含毫秒）
        file_fmt = (
            "%(asctime)s.%(msecs)03d "
            "[%(levelname)-5s] "
            "[%(name)s] "
            "%(message)s"
        )
        file_handler.setFormatter(logging.Formatter(file_fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        root_logger.addHandler(file_handler)

    # 防止传播到 root logger（避免重复输出）
    root_logger.propagate = False


# ============================================================
# 交互追踪上下文
# ============================================================

class TraceContext:
    """线程安全的交互追踪上下文

    每次用户消息触发一个 trace_id，贯穿：
      触发 → 上下文构建 → agent_loop(多轮工具调用) → 回复发送
    """

    def __init__(self):
        self._current: dict[str, str] = {}

    def new(self) -> str:
        """生成新的追踪 ID 并设为当前"""
        trace_id = uuid.uuid4().hex[:12]
        self._current["trace_id"] = trace_id
        return trace_id

    @property
    def current(self) -> str:
        """当前追踪 ID"""
        return self._current.get("trace_id", "----")

    def set_context(self, **kwargs):
        """设置额外上下文字段（如 channel_id, user_id）"""
        self._current.update(kwargs)

    def clear(self):
        """清除当前上下文"""
        self._current.clear()

    def prefix(self) -> str:
        """生成日志前缀字符串，用于嵌入消息中"""
        parts = [f"trace={self.current}"]
        for k, v in self._current.items():
            if k != "trace_id":
                parts.append(f"{k}={v}")
        return "[" + " ".join(parts) + "]"


# 全局单例
trace = TraceContext()
