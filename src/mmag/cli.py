"""
CLI 入口
"""

import asyncio

from .application import Agent
from .config import config
from .logger import get_logger, init_logging, shutdown_logging

log = get_logger(__name__)


async def _run_agent() -> None:
    agent = Agent()
    try:
        await agent.start()
    finally:
        await agent.stop()


def main() -> None:
    """Synchronous console-script entry point."""
    init_logging(
        level=config.log_level,
        log_dir=config.log_dir,
        retain_days=config.log_retention_days,
        log_format=config.log_format,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )
    try:
        asyncio.run(_run_agent())
    except KeyboardInterrupt:
        log.info("👋 收到中断信号，已停止")
    except Exception as e:
        log.error("Agent 异常退出: %s", e, exc_info=True)
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
