"""
CLI 入口
"""

import asyncio

from .agent import Agent
from .config import config
from .logger import get_logger, init_logging

log = get_logger(__name__)


async def main():
    # 统一日志初始化（控制台 + 按日期分文件 + 自动清理）
    init_logging(
        level=config.log_level,
        log_dir=config.log_dir or None,
        retain_days=config.log_retention_days,
    )

    agent = Agent()
    try:
        # 首次启动 (无条件执行)，之后根据 running 状态决定是否重连
        while True:
            await agent.start()
            if not agent.running:
                break
    except KeyboardInterrupt:
        log.info("👋 收到中断信号，正在停止...")
        await agent.stop()
    except Exception as e:
        log.error("Agent 异常退出: %s", e, exc_info=True)
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
