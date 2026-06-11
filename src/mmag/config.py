"""
配置加载 (优先级: .env > 系统环境变量)
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=True)
else:
    print(f"[WARN] .env 文件不存在: {_ENV_PATH}，将使用默认值/环境变量")


def _log_config_loading():
    """打印关键配置项，方便调试"""
    import logging
    keys = [
        "MM_URL", "MM_TOKEN", "MM_TEAM_ID", "MM_CHANNEL_ID",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
        "BOT_NAME", "LISTEN_PROBABILITY", "LOG_LEVEL",
    ]
    log = logging.getLogger("agent")
    log.info("═══ 配置加载 ═══")
    for k in keys:
        v = os.getenv(k, "")
        if "KEY" in k or "TOKEN" in k:
            v = f"{v[:8]}...{v[-4:]}" if len(v) > 12 else "(未设置)" if not v else "(已设置)"
        log.info(f"  {k} = {v}")
    log.info(f"  .env path = {_ENV_PATH} ({'✅ 存在' if _ENV_PATH.exists() else '❌ 不存在'})")
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
    max_context_messages: int = int(os.getenv("MAX_CONTEXT_MESSAGES", "30"))
    typing_delay_min: float = float(os.getenv("TYPING_DELAY_MIN", "1"))
    typing_delay_max: float = float(os.getenv("TYPING_DELAY_MAX", "3"))
    memory_db_path: str = os.getenv("MEMORY_DB_PATH", "./agent_memory.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def ws_url(self) -> str:
        return self.mm_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/v4/websocket"

    @property
    def api_base(self) -> str:
        return f"{self.mm_url}/api/v4"

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.mm_token}", "Content-Type": "application/json"}


config = Config()
