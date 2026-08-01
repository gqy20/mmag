"""
CLI 入口
"""

import asyncio

from .application import Agent
from .config import config
from .logger import get_logger, init_logging

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
        log_dir=config.log_dir or None,
        retain_days=config.log_retention_days,
    )
    try:
        asyncio.run(_run_agent())
    except KeyboardInterrupt:
        log.info("👋 收到中断信号，已停止")
    except Exception as e:
        log.error("Agent 异常退出: %s", e, exc_info=True)


if __name__ == "__main__":
    main()
