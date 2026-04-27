#!/usr/bin/env python3
"""
Claude Remote Agent - 主入口
"""
import asyncio
import signal
import sys
import argparse
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from log_config import setup_logging
from config import config
from agent_client import ClaudeRemoteAgent

import logging
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


async def main():
    """主函数"""
    args = parse_args()

    # 设置日志级别
    if args.debug:
        config.log.level = "DEBUG"

    # 配置日志
    setup_logging()

    logger.info("=" * 50)
    logger.info("  Claude Remote Agent 启动")
    logger.info(f"  版本: {config.VERSION}")
    logger.info(f"  客户端ID: {args.client_id or config.agent.client_id}")
    logger.info(f"  Claude版本: {config.get_claude_version()}")
    logger.info("=" * 50)

    # 创建代理客户端
    agent = ClaudeRemoteAgent(
        server_url=args.server,
        agent_token=args.token,
        client_id=args.client_id
    )

    # 注册信号处理
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(agent.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # 启动代理
    try:
        await agent.start()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        logger.info("Agent stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
