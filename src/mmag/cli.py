"""
CLI 入口
"""

import asyncio

from .config import config
from .logger import init_logging, get_logger
from .agent import Agent

log = get_logger(__name__)


async def main():
    # 统一日志初始化（控制台 + 可选文件）
    init_logging(
        level=config.log_level,
        log_file=config.log_file or None,
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
