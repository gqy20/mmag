"""
统一日志系统 — 分层 Logger + 时间戳分文件 + 自动清理 + 交互追踪

设计原则:
  - 每个模块用自己的 logger 名 (mmag.config, mmag.llm, mmag.tools ...)
  - 统一初始化，不再依赖 cli.py 的 if __name__
  - 控制台 + 文件双输出
  - 日志文件按**启动时间戳**命名: logs/mmag-2026-06-11_103549.log
    → 每次启动独立文件，快速迭代时互不干扰，排查一目了然
  - 超期日志自动清理 (默认保留 30 天)
  - 每次用户交互带 trace_id，贯穿全链路

目录结构:
  ./logs/
  ├── mmag-2026-06-09_082301.log   ← 9 号的某次启动
  ├── mmag-2026-06-10_143022.log   ← 10 号的某次启动
  ├── mmag-2026-06-11_103549.log   ← 今天这次启动（当前写入）
  └── ...                          ← 超过 retain_days 的自动删除
"""

from __future__ import annotations

import glob
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

# 包根名
PKG_NAME = "mmag"

# 全局标志：是否已初始化
_initialized = False

# 默认值
_DEFAULT_LOG_DIR = "logs"
_DEFAULT_RETAIN_DAYS = 30


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
    log_dir: str | None = None,
    retain_days: int = _DEFAULT_RETAIN_DAYS,
    fmt: str | None = None,
    datefmt: str | None = None,
) -> None:
    """统一初始化日志系统（幂等，多次调用只生效一次）

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        log_dir: 日志目录路径（None 则不写文件，默认 "./logs"）
        retain_days: 日志保留天数（0 = 不清理）
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

    # ---- 控制台 handler ----
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(_fmt, datefmt=_datefmt))
    root_logger.addHandler(console)

    # ---- 文件 handler（按日期分割）----
    effective_dir = log_dir or _DEFAULT_LOG_DIR
    if effective_dir:
        log_path = _get_session_log_path(effective_dir)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)

        # 文件用更详细的格式（含毫秒 + 完整日期）
        file_fmt = "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)s] %(message)s"
        file_handler.setFormatter(logging.Formatter(file_fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        root_logger.addHandler(file_handler)

        # 异步清理过期日志（不阻塞启动）
        if retain_days > 0:
            _cleanup_old_logs(effective_dir, retain_days)

    # 防止传播到 root logger（避免重复输出）
    root_logger.propagate = False

    log = get_logger(__name__)
    log.info(
        "日志系统就绪 | 级别=%s | 目录=%s | 保留%d天", level, effective_dir or "(无)", retain_days
    )


def _get_session_log_path(log_dir: str) -> Path:
    """生成本次启动的日志文件路径: {log_dir}/mmag-YYYY-MM-DD_HHMMSS.log

    用时间戳而非纯日期，确保每次重启生成独立文件，
    开发阶段快速迭代时各次运行互不干扰。
    """
    dir_path = Path(log_dir)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return dir_path / f"{PKG_NAME}-{timestamp}.log"


def _cleanup_old_logs(log_dir: str, retain_days: int) -> None:
    """清理超过 retain_days 的旧日志文件（retain_days=0 表示不清理）

    时间戳命名模式下，从文件名解析日期，精确判断是否超期。
    """
    if retain_days <= 0:
        return

    from datetime import timedelta

    dir_path = Path(log_dir)
    pattern = str(dir_path / f"{PKG_NAME}-*.log")
    cutoff = datetime.now() - timedelta(days=retain_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    removed = 0
    for f in glob.glob(pattern):
        # 从文件名提取日期部分: mmag-2026-06-11_103549.log → 2026-06-11
        fname = Path(f).stem  # mmag-2026-06-11_103549
        # 兼容两种格式: 带时间戳 (YYYY-MM-DD_HHMMSS) 和纯日期 (YYYY-MM-DD)
        date_part = (
            fname.split("_")[0].split("-", 1)[-1] if "_" in fname else fname.split("-", 1)[-1]
        )
        # 完整日期: 取 stem 中 PKG_NAME- 后面的全部
        date_part = fname[len(PKG_NAME) + 1 :]  # "2026-06-11_103549" 或 "2026-06-11"
        file_date = date_part.split("_")[0]  # "2026-06-11"

        if file_date < cutoff_str:
            try:
                Path(f).unlink()
                removed += 1
            except OSError:
                pass

    if removed:
        log = get_logger(__name__)
        log.debug(
            "已清理 %d 个过期日志文件 (保留 %d 天, 截止 %s)", removed, retain_days, cutoff_str
        )


def list_log_files(log_dir: str | None = None) -> list[Path]:
    """列出所有日志文件（用于排查时快速定位）"""
    effective_dir = log_dir or _DEFAULT_LOG_DIR
    dir_path = Path(effective_dir)
    if not dir_path.exists():
        return []
    return sorted(dir_path.glob(f"{PKG_NAME}-*.log"), reverse=True)


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
