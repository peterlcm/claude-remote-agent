#!/usr/bin/env python3
"""
Claude Remote Agent - 主入口
"""
import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# Windows 上必须使用 ProactorEventLoop 才能支持 asyncio.create_subprocess_exec
# 与 stdin/stdout/stderr 管道通信。Python 3.8+ 已经默认 Proactor，但显式设置
# 让运行时行为在所有版本下一致。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from log_config import setup_logging  # noqa: E402
from config import config  # noqa: E402
from agent_client import ClaudeRemoteAgent  # noqa: E402

logger = logging.getLogger(__name__)


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Claude Remote Agent - 云端长连接代理"
    )
    parser.add_argument(
        "--server", "-s",
        help=f"WebSocket服务端地址 (默认: {config.agent.server_url})"
    )
    parser.add_argument(
        "--token", "-t",
        help="认证Token"
    )
    parser.add_argument(
        "--client-id", "-c",
        help=f"客户端ID (默认: {config.agent.client_id})"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="启用调试日志"
    )
    return parser.parse_args()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop,
                             agent: ClaudeRemoteAgent) -> None:
    """跨平台安装信号处理器。Windows 不支持 add_signal_handler，回退到 signal.signal。"""

    def _trigger_shutdown() -> None:
        logger.info("Received shutdown signal")
        asyncio.run_coroutine_threadsafe(agent.shutdown(), loop)

    if sys.platform == "win32":
        try:
            signal.signal(signal.SIGINT, lambda *_: _trigger_shutdown())
            signal.signal(signal.SIGTERM, lambda *_: _trigger_shutdown())
        except (ValueError, OSError) as exc:
            logger.debug("signal.signal install failed on win32: %s", exc)
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _trigger_shutdown())


async def main():
    args = parse_args()

    if args.debug:
        config.log.level = "DEBUG"

    setup_logging()

    logger.info("=" * 50)
    logger.info("  Claude Remote Agent 启动")
    logger.info(f"  版本: {config.VERSION}")
    logger.info(f"  客户端ID: {args.client_id or config.agent.client_id}")
    logger.info(f"  Claude版本: {config.get_claude_version()}")
    logger.info(f"  平台: {sys.platform}")
    logger.info("=" * 50)

    agent = ClaudeRemoteAgent(
        server_url=args.server,
        agent_token=args.token,
        client_id=args.client_id,
    )

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, agent)

    try:
        await agent.start()
    except Exception as exc:
        logger.exception(f"Fatal error: {exc}")
        sys.exit(1)
    finally:
        logger.info("Agent stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        sys.exit(1)
