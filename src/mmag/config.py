"""
配置加载 (优先级: .env > 系统环境变量)
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .logger import get_logger

log = get_logger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=True)
else:
    print(f"[WARN] .env 文件不存在: {_ENV_PATH}，将使用默认值/环境变量")


def _log_config_loading():
    """打印关键配置项，方便调试"""
    keys = [
        "MM_URL",
        "MM_TOKEN",
        "MM_TEAM_ID",
        "MM_CHANNEL_ID",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "BOT_NAME",
        "LISTEN_PROBABILITY",
        "MAX_CONTEXT_MESSAGES",
        "MAX_CONTEXT_CHARS",
        "LOG_LEVEL",
        "LOG_DIR",
        "LOG_RETENTION_DAYS",
        "MEMORY_CACHE_MAX",
        "MEMORY_SUMMARY_INTERVAL",
        "MEMORY_COMPACTION_KEEP",
        "MEMORY_SUMMARY_BATCH",
        "MEMORY_CONTEXT_WINDOW",
    ]
    log.info("═══ 配置加载 ═══")
    for k in keys:
        v = os.getenv(k, "")
        if "KEY" in k or "TOKEN" in k:
            v = f"{v[:8]}...{v[-4:]}" if len(v) > 12 else "(未设置)" if not v else "(已设置)"
        log.info("  %s = %s", k, v)
    log.info("  .env path = %s (%s)", _ENV_PATH, "✅ 存在" if _ENV_PATH.exists() else "❌ 不存在")
    log.info("═══════════════")


@dataclass
class Config:
    """从 .env 加载配置"""

    # Mattermost
    mm_url: str = os.getenv("MM_URL", "http://localhost:8065").rstrip("/")
    mm_token: str = os.getenv("MM_TOKEN", "")
    mm_bot_user_id: str = os.getenv("MM_BOT_USER_ID", "")
    mm_team_id: str = os.getenv("MM_TEAM_ID", "")
    mm_channel_id: str = os.getenv("MM_CHANNEL_ID", "")  # 指定频道 ID，留空=监听 Team 下所有频道
    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    anthropic_base_url: str | None = os.getenv("ANTHROPIC_BASE_URL") or None
    # Agent
    bot_name: str = os.getenv("BOT_NAME", "小智")
    bot_display_name: str = os.getenv("BOT_DISPLAY_NAME", "小智")
    listen_probability: float = float(os.getenv("LISTEN_PROBABILITY", "0.15"))
    max_context_messages: int = int(
        os.getenv("MAX_CONTEXT_MESSAGES", "100")
    )  # LLM 上下文窗口消息数
    max_context_chars: int = int(
        os.getenv("MAX_CONTEXT_CHARS", "10000")
    )  # LLM 上下文窗口总字符上限
    typing_delay_min: float = float(os.getenv("TYPING_DELAY_MIN", "1"))
    typing_delay_max: float = float(os.getenv("TYPING_DELAY_MAX", "3"))
    memory_db_path: str = os.getenv("MEMORY_DB_PATH", "./agent_memory.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_dir: str = os.getenv("LOG_DIR", "logs")  # 日志目录（空则不写文件）
    log_retention_days: int = int(os.getenv("LOG_RETENTION_DAYS", "30"))  # 日志保留天数
    # ── 记忆系统 (Layer 1 + Layer 2) ──
    memory_cache_max: int = int(
        os.getenv("MEMORY_CACHE_MAX", "100000")
    )  # 每频道缓存消息上限 (约70MB)
    memory_summary_interval: int = int(
        os.getenv("MEMORY_SUMMARY_INTERVAL", "100")
    )  # 每 N 条触发一次摘要
    memory_compaction_keep: int = int(
        os.getenv("MEMORY_COMPACTION_KEEP", "80000")
    )  # 超限清理后保留条数 (80%)
    memory_summary_batch: int = int(os.getenv("MEMORY_SUMMARY_BATCH", "50"))  # LLM 摘要每批消息数
    memory_context_window: int = int(
        os.getenv("MEMORY_CONTEXT_WINDOW", "100")
    )  # 摘要时注入的前序消息数量（保持上下文连贯）

    @property
    def ws_url(self) -> str:
        return (
            self.mm_url.replace("http://", "ws://").replace("https://", "wss://")
            + "/api/v4/websocket"
        )

    @property
    def api_base(self) -> str:
        return f"{self.mm_url}/api/v4"

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.mm_token}", "Content-Type": "application/json"}


config = Config()
