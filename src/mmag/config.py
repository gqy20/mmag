"""
配置加载 (优先级: .env > 系统环境变量)
"""

import os
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

from .logger import get_logger

log = get_logger(__name__)

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=True)
else:
    print(f"[WARN] .env 文件不存在: {_ENV_PATH}，将使用默认值/环境变量")


# 字段名（小写 snake_case）→ 环境变量名（大写）的映射
# 用 dict 显式列出，避免命名约定变化时反射错位
_FIELD_TO_ENV: dict[str, str] = {
    "mm_url": "MM_URL",
    "mm_token": "MM_TOKEN",
    "mm_bot_user_id": "MM_BOT_USER_ID",
    "mm_team_id": "MM_TEAM_ID",
    "mm_channel_id": "MM_CHANNEL_ID",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "anthropic_model": "ANTHROPIC_MODEL",
    "anthropic_base_url": "ANTHROPIC_BASE_URL",
    "bot_name": "BOT_NAME",
    "bot_display_name": "BOT_DISPLAY_NAME",
    "listen_probability": "LISTEN_PROBABILITY",
    "max_context_messages": "MAX_CONTEXT_MESSAGES",
    "max_context_chars": "MAX_CONTEXT_CHARS",
    "typing_delay_min": "TYPING_DELAY_MIN",
    "typing_delay_max": "TYPING_DELAY_MAX",
    "memory_db_path": "MEMORY_DB_PATH",
    "log_level": "LOG_LEVEL",
    "log_dir": "LOG_DIR",
    "log_retention_days": "LOG_RETENTION_DAYS",
    "memory_summary_interval": "MEMORY_SUMMARY_INTERVAL",
    "memory_context_window": "MEMORY_CONTEXT_WINDOW",
}
# 敏感字段（值要脱敏打印，只显示前后几位）
_SECRET_FIELD_NAMES: frozenset[str] = frozenset({"mm_token", "anthropic_api_key"})


def _log_config_loading():
    """遍历 Config 字段打印加载结果，新增/删除配置项时无需改这里"""
    log.info("═══ 配置加载 ═══")
    for f in fields(Config):
        env_key = _FIELD_TO_ENV.get(f.name)
        if env_key is None:
            # 反射到的字段不在映射表里，跳过（可能是新加字段忘了登记）
            continue
        v = os.getenv(env_key, "")
        if f.name in _SECRET_FIELD_NAMES:
            v = f"{v[:8]}...{v[-4:]}" if len(v) > 12 else (v or "(未设置)")
        log.info("  %s = %s", env_key, v)
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
    # message_log 永久存储,无容量上限;摘要按消息条数触发
    memory_summary_interval: int = int(
        os.getenv("MEMORY_SUMMARY_INTERVAL", "100")
    )  # 每 N 条触发一次摘要
    memory_context_window: int = int(
        os.getenv("MEMORY_CONTEXT_WINDOW", "100")
    )  # 摘要时注入的前序消息数量（保持上下文连贯）

    @property
    def ws_url(self) -> str:
        return (
            self.mm_url.replace("http://", "ws://").replace("https://", "wss://")
            + "/api/v4/websocket"
        )


config = Config()
