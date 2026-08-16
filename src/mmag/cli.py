"""
CLI 入口
"""

import asyncio
import contextlib
import signal

from .application import Agent
from .config import config
from .logger import get_logger, init_logging, shutdown_logging

log = get_logger(__name__)


async def _watch_shutdown(agent: Agent, requested: asyncio.Event) -> None:
    await requested.wait()
    while agent.ws is None:
        await asyncio.sleep(0.05)
    await agent.ws.close()


async def _run_agent() -> bool:
    agent = Agent()
    requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, requested.set)
            installed_signals.append(name)
        except (NotImplementedError, RuntimeError):
            pass
    shutdown_watcher = asyncio.create_task(
        _watch_shutdown(agent, requested), name="process-shutdown"
    )
    try:
        await agent.start()
    except asyncio.CancelledError:
        # Fallback for platforms where loop signal handlers are unavailable.
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
    finally:
        shutdown_watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_watcher
        await agent.stop()
        for name in installed_signals:
            loop.remove_signal_handler(name)
    return requested.is_set()


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
        interrupted = asyncio.run(_run_agent())
        if interrupted:
            log.info("👋 收到中断信号，已停止")
    except KeyboardInterrupt:
        log.info("👋 收到中断信号，已停止")
    except Exception as e:
        log.error("Agent 异常退出: %s", e, exc_info=True)
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
