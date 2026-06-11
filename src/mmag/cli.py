"""
CLI 入口
"""

import asyncio
import logging

from .config import config
from .agent import Agent

log = logging.getLogger("agent")


async def main():
    agent = Agent()
    try:
        # 首次启动 (无条件执行)，之后根据 running 状态决定是否重连
        while True:
            await agent.start()
            if not agent.running:
                break
    except KeyboardInterrupt:
        log.info("\n👋 收到中断信号，正在停止...")
        await agent.stop()
    except Exception as e:
        log.error(f"Agent 异常退出: {e}", exc_info=True)
        await agent.stop()


if __name__ == "__main__":
    # 日志初始化 (必须在 config 加载之后)
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())
