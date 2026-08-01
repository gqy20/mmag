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
_SOURCE_AGENT_PACKAGES = Path(__file__).resolve().parents[2] / "agents"
_INSTALLED_AGENT_PACKAGES = Path(__file__).resolve().parent / "agents"
_DEFAULT_AGENT_PACKAGES = (
    _SOURCE_AGENT_PACKAGES if _SOURCE_AGENT_PACKAGES.is_dir() else _INSTALLED_AGENT_PACKAGES
)
_SOURCE_SKILL_PACKAGES = Path(__file__).resolve().parents[2] / "skills"
_INSTALLED_SKILL_PACKAGES = Path(__file__).resolve().parent / "skills"
_DEFAULT_SKILL_PACKAGES = (
    _SOURCE_SKILL_PACKAGES if _SOURCE_SKILL_PACKAGES.is_dir() else _INSTALLED_SKILL_PACKAGES
)
_SOURCE_POLICIES = Path(__file__).resolve().parents[2] / "policies"
_INSTALLED_POLICIES = Path(__file__).resolve().parent / "policies"
_DEFAULT_POLICIES = _SOURCE_POLICIES if _SOURCE_POLICIES.is_dir() else _INSTALLED_POLICIES
_SOURCE_MODEL_POLICIES = Path(__file__).resolve().parents[2] / "model-policies"
_INSTALLED_MODEL_POLICIES = Path(__file__).resolve().parent / "model-policies"
_DEFAULT_MODEL_POLICIES = (
    _SOURCE_MODEL_POLICIES if _SOURCE_MODEL_POLICIES.is_dir() else _INSTALLED_MODEL_POLICIES
)
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=True)
else:
    print(f"[WARN] .env 文件不存在: {_ENV_PATH}，将使用默认值/环境变量")


# 字段名（小写 snake_case）→ 环境变量名（大写）的映射
# 用 dict 显式列出，避免命名约定变化时反射错位
_FIELD_TO_ENV: dict[str, str] = {
    "mm_url": "MM_URL",
    "mm_token": "MM_TOKEN",
    "mm_team_id": "MM_TEAM_ID",
    "mm_channel_id": "MM_CHANNEL_ID",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "anthropic_model": "ANTHROPIC_MODEL",
    "anthropic_base_url": "ANTHROPIC_BASE_URL",
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
    "max_images_per_msg": "MAX_IMAGES_PER_MSG",
    "max_image_bytes": "MAX_IMAGE_BYTES",
    "max_text_attachment_chars": "MAX_TEXT_ATTACHMENT_CHARS",
    "max_tool_rounds": "MAX_TOOL_ROUNDS",
    "use_sdk_llm": "USE_SDK_LLM",
    "mcp_allowed_tools": "MCP_ALLOWED_TOOLS",
    "pipeline_max_concurrency": "PIPELINE_MAX_CONCURRENCY",
    "pipeline_max_pending": "PIPELINE_MAX_PENDING",
    "runtime_deadline_seconds": "RUNTIME_DEADLINE_SECONDS",
    "model_budget_usd": "MODEL_BUDGET_USD",
    "agent_packages_path": "AGENT_PACKAGES_PATH",
    "skill_packages_path": "SKILL_PACKAGES_PATH",
    "policies_path": "POLICIES_PATH",
    "model_policies_path": "MODEL_POLICIES_PATH",
}
# 敏感字段（日志只记录是否配置，不输出任何值片段）
_SECRET_FIELD_NAMES: frozenset[str] = frozenset({"mm_token", "anthropic_api_key"})


def _secret_status(value: str | None) -> str:
    """Return presence-only status so logs never contain secret fragments."""
    return "(已设置)" if value else "(未设置)"


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
            v = _secret_status(v)
        log.info("  %s = %s", env_key, v)
    log.info("  .env path = %s (%s)", _ENV_PATH, "✅ 存在" if _ENV_PATH.exists() else "❌ 不存在")
    log.info("═══════════════")


@dataclass
class Config:
    """从 .env 加载配置"""

    # Mattermost
    mm_url: str = os.getenv("MM_URL", "http://localhost:8065").rstrip("/")
    mm_token: str = os.getenv("MM_TOKEN", "")
    mm_team_id: str = os.getenv("MM_TEAM_ID", "")
    mm_channel_id: str = os.getenv("MM_CHANNEL_ID", "")  # 指定频道 ID，留空=监听 Team 下所有频道
    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    anthropic_base_url: str | None = os.getenv("ANTHROPIC_BASE_URL") or None
    # Agent
    max_context_messages: int = int(
        os.getenv("MAX_CONTEXT_MESSAGES", "100")
    )  # LLM 上下文窗口消息数
    max_context_chars: int = int(
        os.getenv("MAX_CONTEXT_CHARS", "30000")
    )  # LLM 上下文窗口总字符上限 (中英混合 1 字符 ≈ 0.5 token → 30000 字符 ≈ 15k token)
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
    # ── 多模态 (图片附件) ──
    # 单条消息最多喂多少张图给 LLM,超过会被跳过(降为附件名文本)。
    # 太多图会爆 token,1 张图 ≈ 1500 token,4 张 ≈ 6000 token。
    max_images_per_msg: int = int(os.getenv("MAX_IMAGES_PER_MSG", "4"))
    # 单张图片字节上限,超过就跳过(避免下载时间过长 / token 过贵)。
    max_image_bytes: int = int(os.getenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))  # 默认 5MB
    # 文本附件 (markdown/txt/json 等) 的字符上限, 超过会截断。
    # 50000 字符 ≈ 12K token, 兼顾完整性与 context 预算。
    max_text_attachment_chars: int = int(os.getenv("MAX_TEXT_ATTACHMENT_CHARS", "50000"))
    # ── Agent Loop ──
    # Agentic 工具调用的最大轮数(每轮 = 1 次 LLM + N 次工具 + 1 次结果回填)。
    # 调高 → 复杂任务可拆更多步,但单次请求耗时和 token 都线性增加;
    # 调低 → 快速失败,长任务会被强制收尾(返回最后一轮文本)。
    max_tool_rounds: int = int(os.getenv("MAX_TOOL_ROUNDS", "10"))  # 默认 10
    # ── SDK LLM ──
    # LangGraph 是默认运行时；Claude Agent SDK 仅在显式开启时替代默认路由。
    use_sdk_llm: bool = os.getenv("USE_SDK_LLM", "false").lower() in ("true", "1", "yes")
    # LangGraph Runtime 的外部 MCP 工具白名单，名称格式为 mcp_<server>_<tool>
    mcp_allowed_tools: tuple[str, ...] = tuple(
        dict.fromkeys(
            name.strip() for name in os.getenv("MCP_ALLOWED_TOOLS", "").split(",") if name.strip()
        )
    )
    pipeline_max_concurrency: int = int(os.getenv("PIPELINE_MAX_CONCURRENCY", "8"))
    pipeline_max_pending: int = int(os.getenv("PIPELINE_MAX_PENDING", "256"))
    runtime_deadline_seconds: float = float(os.getenv("RUNTIME_DEADLINE_SECONDS", "120"))
    model_budget_usd: float = float(os.getenv("MODEL_BUDGET_USD", "100"))
    agent_packages_path: str = os.getenv("AGENT_PACKAGES_PATH", str(_DEFAULT_AGENT_PACKAGES))
    skill_packages_path: str = os.getenv("SKILL_PACKAGES_PATH", str(_DEFAULT_SKILL_PACKAGES))
    policies_path: str = os.getenv("POLICIES_PATH", str(_DEFAULT_POLICIES))
    model_policies_path: str = os.getenv("MODEL_POLICIES_PATH", str(_DEFAULT_MODEL_POLICIES))

    @property
    def ws_url(self) -> str:
        return (
            self.mm_url.replace("http://", "ws://").replace("https://", "wss://")
            + "/api/v4/websocket"
        )


config = Config()
